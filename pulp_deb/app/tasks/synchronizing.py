import asyncio
import aiohttp
import os
import shutil
import bz2
import gzip
import lzma
import gnupg

from collections import defaultdict
from tempfile import NamedTemporaryFile
from debian import deb822
from urllib.parse import urlparse, urlunparse
from django.conf import settings

from pulpcore.plugin.exceptions import DigestValidationError

from pulpcore.plugin.models import (
    Artifact,
    ProgressReport,
    Remote,
    Repository,
)

from pulpcore.plugin.stages import (
    DeclarativeArtifact,
    DeclarativeContent,
    DeclarativeVersion,
    Stage,
    QueryExistingArtifacts,
    ArtifactDownloader,
    ArtifactSaver,
    QueryExistingContents,
    ContentSaver,
    RemoteArtifactSaver,
    ResolveContentFutures,
)

from pulp_deb.app.models import (
    GenericContent,
    Release,
    ReleaseArchitecture,
    ReleaseComponent,
    ReleaseFile,
    PackageIndex,
    InstallerFileIndex,
    Package,
    PackageReleaseComponent,
    InstallerPackage,
    AptRemote,
)

from pulp_deb.app.serializers import (
    InstallerPackage822Serializer,
    Package822Serializer,
)

from pulp_deb.app.constants import (
    NO_MD5_WARNING_MESSAGE,
    CHECKSUM_TYPE_MAP,
)


import logging
from gettext import gettext as _

log = logging.getLogger(__name__)


class NoReleaseFile(Exception):
    """
    Exception to signal, that no file representing a release is present.
    """

    def __init__(self, distribution, *args, **kwargs):
        """
        Exception to signal, that no file representing a release is present.
        """
        super().__init__(
            "No valid Release file found for '{}'.".format(distribution), *args, **kwargs
        )


class NoPackageIndexFile(Exception):
    """
    Exception to signal, that no file representing a package index is present.
    """

    def __init__(self, relative_dir, *args, **kwargs):
        """
        Exception to signal, that no file representing a package index is present.
        """
        super().__init__(
            "No suitable Package index file found in '{}'.".format(relative_dir), *args, **kwargs
        )

    pass


def synchronize(remote_pk, repository_pk, mirror):
    """
    Sync content from the remote repository.

    Create a new version of the repository that is synchronized with the remote.

    Args:
        remote_pk (str): The remote PK.
        repository_pk (str): The repository PK.
        mirror (bool): True for mirror mode, False for additive.

    Raises:
        ValueError: If the remote does not specify a URL to sync

    """
    remote = AptRemote.objects.get(pk=remote_pk)
    repository = Repository.objects.get(pk=repository_pk)

    if not remote.url:
        raise ValueError(_("A remote must have a url specified to synchronize."))

    first_stage = DebFirstStage(remote)
    DebDeclarativeVersion(first_stage, repository, mirror=mirror).create()


class DeclarativeFailsafeArtifact(DeclarativeArtifact):
    """
    A declarative artifact that does not fail on 404.
    """

    async def download(self):
        """
        Download the artifact and set to None on 404.
        """
        try:
            await super().download()
        except aiohttp.client_exceptions.ClientResponseError as e:
            if e.code == 404:
                self.artifact = None
                log.info(
                    _("Artifact with relative_path='{}' not found. Ignored").format(
                        self.relative_path
                    )
                )
            else:
                raise
        except DigestValidationError:
            self.artifact = None
            log.info(
                _("Digest for artifact with relative_path='{}' not matched. Ignored").format(
                    self.relative_path
                )
            )


class DebDeclarativeVersion(DeclarativeVersion):
    """
    This class creates the Pipeline.
    """

    def pipeline_stages(self, new_version):
        """
        Build the list of pipeline stages feeding into the ContentAssociation stage.

        Args:
            new_version (:class:`~pulpcore.plugin.models.RepositoryVersion`): The
                new repository version that is going to be built.

        Returns:
            list: List of :class:`~pulpcore.plugin.stages.Stage` instances

        """
        pipeline = [
            self.first_stage,
            QueryExistingArtifacts(),
            ArtifactDownloader(),
            DebDropFailedArtifacts(),
            ArtifactSaver(),
            DebUpdateReleaseFileAttributes(remote=self.first_stage.remote),
            DebUpdatePackageIndexAttributes(),
            QueryExistingContents(),
            ContentSaver(),
            RemoteArtifactSaver(),
            ResolveContentFutures(),
        ]
        return pipeline


def _filter_split(values, filter_values, value_type):
    """
    Returns the intersection of two strings of whitespace separated elements as a sorted set.
    If an element of values.split() has a path prefix, it is still considered to be equal to an
    element of filter_values.split() without the path prefix. E.g.: The intersection/return value
    of values="updates/main" and filter_values="main" is considered to be set(["updates/main"]).
    If a filter value provided does not correspond to any value, a warning is logged.
    """
    value_list = values.split()
    if not filter_values:
        filtered_values = value_list
    else:
        filter_value_list = filter_values.split()
        filtered_values = [
            value
            for value in value_list
            if value in filter_value_list or os.path.basename(value) in filter_value_list
        ]

        # Log any filter values that do not correspont to any value.
        plain_value_list = [os.path.basename(value) for value in value_list]
        for filter_value in filter_value_list:
            if filter_value not in value_list and filter_value not in plain_value_list:
                message = (
                    "{0}='{1}' not amongst the release file {0}s '{2}'. "
                    "This often indicates a misspelled {0} in the remote being used."
                )
                log.warning(_(message).format(value_type, filter_value, values))

    return sorted(set(filtered_values))


class DebUpdateReleaseFileAttributes(Stage):
    """
    This stage handles ReleaseFile content.

    It also transfers the sha256 from the artifact to the ReleaseFile content units.
    """

    def __init__(self, remote, *args, **kwargs):
        """Initialize DebUpdateReleaseFileAttributes stage."""
        super().__init__(*args, **kwargs)
        self.remote = remote
        self.gpgkey = remote.gpgkey
        if self.gpgkey:
            gnupghome = os.path.join(os.getcwd(), "gpg-home")
            os.makedirs(gnupghome)
            self.gpg = gnupg.GPG(gpgbinary="/usr/bin/gpg", gnupghome=gnupghome)
            import_res = self.gpg.import_keys(self.gpgkey)
            if import_res.count == 0:
                log.warning(_("Key import failed."))
            pass

    async def run(self):
        """
        Parse ReleaseFile content units.

        Update release content with information obtained from its artifact.
        """
        with ProgressReport(message="Update ReleaseFile units", code="update.release_file") as pb:
            async for d_content in self.items():
                if isinstance(d_content.content, ReleaseFile):
                    release_file = d_content.content
                    da_names = {
                        os.path.basename(da.relative_path): da for da in d_content.d_artifacts
                    }
                    if "Release" in da_names:
                        if "Release.gpg" in da_names:
                            if self.gpgkey:
                                with NamedTemporaryFile() as tmp_file:
                                    tmp_file.write(da_names["Release"].artifact.file.read())
                                    tmp_file.flush()
                                    verified = self.gpg.verify_file(
                                        da_names["Release.gpg"].artifact.file, tmp_file.name
                                    )
                                if verified.valid:
                                    log.info(_("Verification of Release successful."))
                                    release_file_artifact = da_names["Release"].artifact
                                    release_file.relative_path = da_names["Release"].relative_path
                                else:
                                    log.warning(_("Verification of Release failed. Dropping it."))
                                    d_content.d_artifacts.remove(da_names.pop("Release"))
                                    d_content.d_artifacts.remove(da_names.pop("Release.gpg"))
                            else:
                                release_file_artifact = da_names["Release"].artifact
                                release_file.relative_path = da_names["Release"].relative_path
                        else:
                            if self.gpgkey:
                                d_content.d_artifacts.delete(da_names["Release"])
                            else:
                                release_file_artifact = da_names["Release"].artifact
                                release_file.relative_path = da_names["Release"].relative_path
                    else:
                        if "Release.gpg" in da_names:
                            # No need to keep the signature without "Release"
                            d_content.d_artifacts.remove(da_names.pop("Release.gpg"))

                    if "InRelease" in da_names:
                        if self.gpgkey:
                            verified = self.gpg.verify_file(da_names["InRelease"].artifact.file)
                            if verified.valid:
                                log.info(_("Verification of InRelease successful."))
                                release_file_artifact = da_names["InRelease"].artifact
                                release_file.relative_path = da_names["InRelease"].relative_path
                            else:
                                log.warning(_("Verification of InRelease failed. Dropping it."))
                                d_content.d_artifacts.remove(da_names.pop("InRelease"))
                        else:
                            release_file_artifact = da_names["InRelease"].artifact
                            release_file.relative_path = da_names["InRelease"].relative_path

                    if not d_content.d_artifacts:
                        # No (proper) artifacts left -> distribution not found
                        raise NoReleaseFile(distribution=release_file.distribution)

                    release_file.sha256 = release_file_artifact.sha256
                    release_file_dict = deb822.Release(release_file_artifact.file)
                    if "codename" in release_file_dict:
                        release_file.codename = release_file_dict["Codename"]
                    if "suite" in release_file_dict:
                        release_file.suite = release_file_dict["Suite"]
                    release_file.components = release_file_dict["Components"]
                    release_file.architectures = release_file_dict["Architectures"]
                    log.debug(_("Codename: {}").format(release_file.codename))
                    log.debug(_("Components: {}").format(release_file.components))
                    log.debug(_("Architectures: {}").format(release_file.architectures))
                    pb.increment()
                await self.put(d_content)


class DebUpdatePackageIndexAttributes(Stage):  # TODO: Needs a new name
    """
    This stage handles PackageIndex content.
    """

    async def run(self):
        """
        Parse PackageIndex content units.

        Ensure, that an uncompressed artifact is available.
        """
        with ProgressReport(message="Update PackageIndex units", code="update.packageindex") as pb:
            async for d_content in self.items():
                if isinstance(d_content.content, PackageIndex):
                    if not d_content.d_artifacts:
                        d_content.content = None
                        d_content.resolve()
                        continue
                    content = d_content.content
                    if not [
                        da for da in d_content.d_artifacts if da.artifact.sha256 == content.sha256
                    ]:
                        # No main_artifact found, uncompress one
                        relative_dir = os.path.dirname(d_content.content.relative_path)
                        filename = _uncompress_artifact(d_content.d_artifacts, relative_dir)
                        da = DeclarativeArtifact(
                            Artifact.init_and_validate(
                                filename, expected_digests={"sha256": content.sha256}
                            ),
                            filename,
                            content.relative_path,
                            d_content.d_artifacts[0].remote,
                        )
                        d_content.d_artifacts.append(da)
                        da.artifact.save()

                    pb.increment()
                await self.put(d_content)


def _uncompress_artifact(d_artifacts, relative_dir):
    for d_artifact in d_artifacts:
        ext = os.path.splitext(d_artifact.relative_path)[1]
        if ext == ".gz":
            compressor = gzip
        elif ext == ".bz2":
            compressor = bz2
        elif ext == ".xz":
            compressor = lzma
        else:
            log.info(_("Compression algorithm unknown for extension '{}'.").format(ext))
            continue
        # At this point we have found a file that can be decompressed
        with NamedTemporaryFile(delete=False) as f_out:
            with compressor.open(d_artifact.artifact.file) as f_in:
                shutil.copyfileobj(f_in, f_out)
        return f_out.name
    # Not one artifact was suitable
    raise NoPackageIndexFile(relative_dir=relative_dir)


class DebDropFailedArtifacts(Stage):
    """
    This stage removes failed failsafe artifacts.

    In case we tried to fetch something, but the artifact 404ed, we simply drop it.
    """

    async def run(self):
        """
        Remove None from d_artifacts in DeclarativeContent units.
        """
        async for d_content in self.items():
            d_content.d_artifacts = [
                d_artifact for d_artifact in d_content.d_artifacts if d_artifact.artifact
            ]
            await self.put(d_content)


class DebFirstStage(Stage):
    """
    The first stage of a pulp_deb sync pipeline.
    """

    def __init__(self, remote, *args, **kwargs):
        """
        The first stage of a pulp_deb sync pipeline.

        Args:
            remote (FileRemote): The remote data to be used when syncing

        """
        super().__init__(*args, **kwargs)
        self.remote = remote
        self.parsed_url = urlparse(remote.url)

    async def run(self):
        """
        Build and emit `DeclarativeContent` from the Release data.
        """
        if "md5" not in settings.ALLOWED_CONTENT_CHECKSUMS and settings.FORBIDDEN_CHECKSUM_WARNINGS:
            log.warning(_(NO_MD5_WARNING_MESSAGE))

        await asyncio.gather(
            *[self._handle_distribution(dist) for dist in self.remote.distributions.split()]
        )

    async def _create_unit(self, d_content):
        await self.put(d_content)
        return await d_content.resolution()

    def _to_d_artifact(self, relative_path, data=None):
        artifact = Artifact(**_get_checksums(data or {}))
        url_path = os.path.join(self.parsed_url.path, relative_path)
        return DeclarativeFailsafeArtifact(
            artifact,
            urlunparse(self.parsed_url._replace(path=url_path)),
            relative_path,
            self.remote,
            deferred_download=False,
        )

    async def _handle_distribution(self, distribution):
        log.info(_('Downloading Release file for distribution: "{}"').format(distribution))
        # Create release_file
        if distribution[-1] == "/":
            release_file_dir = distribution.strip("/")
        else:
            release_file_dir = os.path.join("dists", distribution)
        release_file_dc = DeclarativeContent(
            content=ReleaseFile(distribution=distribution),
            d_artifacts=[
                self._to_d_artifact(os.path.join(release_file_dir, filename))
                for filename in ["Release", "InRelease", "Release.gpg"]
            ],
        )
        release_file = await self._create_unit(release_file_dc)
        if release_file is None:
            return
        # Create release object
        release_unit = Release(
            codename=release_file.codename, suite=release_file.suite, distribution=distribution
        )
        release_dc = DeclarativeContent(content=release_unit)
        release = await self._create_unit(release_dc)
        # Create release architectures
        for architecture in _filter_split(
            release_file.architectures, self.remote.architectures, "architecture"
        ):
            release_architecture_dc = DeclarativeContent(
                content=ReleaseArchitecture(architecture=architecture, release=release)
            )
            await self.put(release_architecture_dc)
        # Parse release file
        log.info(_('Parsing Release file at distribution="{}"').format(distribution))
        release_file_dict = deb822.Release(release_file.main_artifact.file)
        # collect file references in new dict
        file_references = defaultdict(deb822.Deb822Dict)
        for digest_name in ["SHA512", "SHA256", "SHA1", "MD5sum"]:
            if digest_name in release_file_dict:
                for unit in release_file_dict[digest_name]:
                    file_references[unit["Name"]].update(unit)
        await asyncio.gather(
            *[
                self._handle_component(component, release, release_file, file_references)
                for component in _filter_split(
                    release_file.components, self.remote.components, "component"
                )
            ]
        )

    async def _handle_component(self, component, release, release_file, file_references):
        # Create release_component
        release_component_dc = DeclarativeContent(
            content=ReleaseComponent(component=component, release=release)
        )
        release_component = await self._create_unit(release_component_dc)
        architectures = _filter_split(
            release_file.architectures, self.remote.architectures, "architecture"
        )
        pending_tasks = []
        # Handle package indices
        pending_tasks.extend(
            [
                self._handle_package_index(
                    release_file, release_component, architecture, file_references
                )
                for architecture in architectures
            ]
        )
        # Handle installer package indices
        if self.remote.sync_udebs:
            pending_tasks.extend(
                [
                    self._handle_package_index(
                        release_file,
                        release_component,
                        architecture,
                        file_references,
                        "debian-installer",
                    )
                    for architecture in architectures
                ]
            )
        # Handle installer file indices
        if self.remote.sync_installer:
            pending_tasks.extend(
                [
                    self._handle_installer_file_index(
                        release_file, release_component, architecture, file_references
                    )
                    for architecture in architectures
                ]
            )
        if self.remote.sync_sources:
            raise NotImplementedError("Syncing source repositories is not yet implemented.")
        await asyncio.gather(*pending_tasks)

    async def _handle_package_index(
        self, release_file, release_component, architecture, file_references, infix=""
    ):
        # Create package_index
        release_base_path = os.path.dirname(release_file.relative_path)
        if release_file.distribution[-1] == "/":
            # Flat repo format
            package_index_dir = ""
        else:
            package_index_dir = os.path.join(
                release_component.plain_component, infix, "binary-{}".format(architecture)
            )
        d_artifacts = []
        for filename in ["Packages", "Packages.gz", "Packages.xz", "Release"]:
            path = os.path.join(package_index_dir, filename)
            if path in file_references:
                relative_path = os.path.join(release_base_path, path)
                d_artifacts.append(self._to_d_artifact(relative_path, file_references[path]))
        if not d_artifacts:
            # No reference here, skip this component architecture combination
            return
        log.info(_("Downloading: {}/Packages").format(package_index_dir))
        content_unit = PackageIndex(
            release=release_file,
            component=release_component.component,
            architecture=architecture,
            sha256=d_artifacts[0].artifact.sha256,
            relative_path=os.path.join(release_base_path, package_index_dir, "Packages"),
        )
        package_index = await self._create_unit(
            DeclarativeContent(content=content_unit, d_artifacts=d_artifacts)
        )
        if not package_index:
            if self.remote.ignore_missing_package_indices:
                log.info(_("No packages index for architecture {}. Skipping.").format(architecture))
                return
            else:
                relative_dir = os.path.join(release_base_path, package_index_dir)
                raise NoPackageIndexFile(relative_dir=relative_dir)
        # Interpret policy to download Artifacts or not
        deferred_download = self.remote.policy != Remote.IMMEDIATE
        # parse package_index
        package_futures = []
        for package_paragraph in deb822.Packages.iter_paragraphs(package_index.main_artifact.file):
            try:
                package_relpath = os.path.normpath(package_paragraph["Filename"])
                package_sha256 = package_paragraph["sha256"]
                if package_relpath.endswith(".deb"):
                    package_class = Package
                    serializer_class = Package822Serializer
                elif package_relpath.endswith(".udeb"):
                    package_class = InstallerPackage
                    serializer_class = InstallerPackage822Serializer
                log.debug(_("Downloading package {}").format(package_paragraph["Package"]))
                serializer = serializer_class.from822(data=package_paragraph)
                serializer.is_valid(raise_exception=True)
                package_content_unit = package_class(
                    relative_path=package_relpath,
                    sha256=package_sha256,
                    **serializer.validated_data,
                )
                package_path = os.path.join(self.parsed_url.path, package_relpath)
                package_da = DeclarativeArtifact(
                    artifact=Artifact(**_get_checksums(package_paragraph)),
                    url=urlunparse(self.parsed_url._replace(path=package_path)),
                    relative_path=package_relpath,
                    remote=self.remote,
                    deferred_download=deferred_download,
                )
                package_dc = DeclarativeContent(
                    content=package_content_unit, d_artifacts=[package_da]
                )
                package_futures.append(package_dc)
                await self.put(package_dc)
            except KeyError:
                log.warning(_("Ignoring invalid package paragraph. {}").format(package_paragraph))
        # Assign packages to this release_component
        for package_future in package_futures:
            package = await package_future.resolution()
            if not isinstance(package, Package):
                # TODO repeat this for installer packages
                continue
            package_release_component_dc = DeclarativeContent(
                content=PackageReleaseComponent(
                    package=package, release_component=release_component
                )
            )
            await self.put(package_release_component_dc)

    async def _handle_installer_file_index(
        self, release_file, release_component, architecture, file_references
    ):
        # Create installer file index
        release_base_path = os.path.dirname(release_file.relative_path)
        installer_file_index_dir = os.path.join(
            release_component.plain_component,
            "installer-{}".format(architecture),
            "current",
            "images",
        )
        d_artifacts = []
        for filename in InstallerFileIndex.FILE_ALGORITHM.keys():
            path = os.path.join(installer_file_index_dir, filename)
            if path in file_references:
                relative_path = os.path.join(release_base_path, path)
                d_artifacts.append(self._to_d_artifact(relative_path, file_references[path]))
        if not d_artifacts:
            return
        log.info(_("Downloading installer files from {}").format(installer_file_index_dir))
        content_unit = InstallerFileIndex(
            release=release_file,
            component=release_component.component,
            architecture=architecture,
            sha256=d_artifacts[0].artifact.sha256,
            relative_path=os.path.join(release_base_path, installer_file_index_dir),
        )
        d_content = DeclarativeContent(content=content_unit, d_artifacts=d_artifacts)
        installer_file_index = await self._create_unit(d_content)
        # Interpret policy to download Artifacts or not
        deferred_download = self.remote.policy != Remote.IMMEDIATE
        # Parse installer file index
        file_list = defaultdict(dict)
        for content_artifact in installer_file_index.contentartifact_set.all():
            algorithm = InstallerFileIndex.FILE_ALGORITHM.get(
                os.path.basename(content_artifact.relative_path)
            )
            if not algorithm:
                continue
            for line in content_artifact.artifact.file:
                digest, filename = line.decode().strip().split(maxsplit=1)
                filename = os.path.normpath(filename)
                if filename in InstallerFileIndex.FILE_ALGORITHM:  # strangely they may appear here
                    continue
                file_list[filename][algorithm] = digest

        for filename, digests in file_list.items():
            relpath = os.path.join(installer_file_index.relative_path, filename)
            urlpath = os.path.join(self.parsed_url.path, relpath)
            content_unit = GenericContent(sha256=digests["sha256"], relative_path=relpath)
            d_artifact = DeclarativeArtifact(
                artifact=Artifact(**digests),
                url=urlunparse(self.parsed_url._replace(path=urlpath)),
                relative_path=relpath,
                remote=self.remote,
                deferred_download=deferred_download,
            )
            d_content = DeclarativeContent(content=content_unit, d_artifacts=[d_artifact])
            await self.put(d_content)

    async def _handle_translation_files(self, release_file, release_component, file_references):
        translation_dir = os.path.join(release_component.plain_component, "i18n")
        paths = [path for path in file_references.keys() if path.startswith(translation_dir)]
        translations = {}
        for path in paths:
            relative_path = os.path.join(os.path.dirname(release_file.relative_path), path)
            d_artifact = self._to_d_artifact(relative_path, file_references[path])
            key, ext = os.path.splitext(relative_path)
            if key not in translations:
                translations[key] = {"sha256": None, "d_artifacts": []}
            if not ext:
                translations[key]["sha256"] = d_artifact.artifact.sha256
            translations[key]["d_artifacts"].append(d_artifact)

        for relative_path, translation in translations.items():
            content_unit = GenericContent(sha256=translation["sha256"], relative_path=relative_path)
            await self.put(
                DeclarativeContent(content=content_unit, d_artifacts=translation["d_artifacts"])
            )


def _get_checksums(unit_dict):
    """
    Filters the unit_dict provided to retain only checksum fields present in the
    CHECKSUM_TYPE_MAP and permitted by ALLOWED_CONTENT_CHECKSUMS. Also translates the
    retained keys from Debian checksum field name to Pulp checksum type name.

    For example, if the following is in the unit_dict:
        'SHA256': '0b412f7b1a25087871c3e9f2743f4d90b9b025e415f825483b6f6a197d11d409',

    The return dict would contain:
        'sha256': '0b412f7b1a25087871c3e9f2743f4d90b9b025e415f825483b6f6a197d11d409',

    This key translation is defined by the CHECKSUM_TYPE_MAP.
    """
    return {
        checksum_type: unit_dict[deb_field]
        for checksum_type, deb_field in CHECKSUM_TYPE_MAP.items()
        if checksum_type in settings.ALLOWED_CONTENT_CHECKSUMS and deb_field in unit_dict
    }
