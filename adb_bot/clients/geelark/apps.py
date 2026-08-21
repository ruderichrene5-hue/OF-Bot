"""Installing apps on a Geelark phone.

Two endpoints that are easy to confuse, and both are per-phone:

* `/app/list` -- what is **already installed** here. Items carry `appVersionId`
  and an `installStatus`.
* `/app/installable/list` -- the **catalogue** of what could be installed here.
  Items nest their versions under `appVersionInfoList`, so the id needed to
  install is one level deeper than it looks.

Instagram is in the catalogue on this account, so a fresh phone can be brought
to a usable state entirely through the API.
"""

from __future__ import annotations

from .transport import GeelarkError, GeelarkTransport

INSTALLED_PATH = "/app/list"
INSTALLABLE_PATH = "/app/installable/list"
INSTALL_PATH = "/app/install"
UNINSTALL_PATH = "/app/uninstall"
START_PATH = "/app/start"
STOP_PATH = "/app/stop"

INSTAGRAM_PACKAGE = "com.instagram.android"

# "app installing" -- returned when an install is already under way on
# this phone, which a freshly created phone does by itself.
CODE_APP_INSTALLING = 42003

# `installStatus` on an installed/installable row.
INSTALL_STATUS = {
    0: "installing",
    1: "installed",
    2: "install failed",
    3: "uninstalling",
    4: "uninstalled",
    5: "uninstall failed",
}


def install_status_label(status: object) -> str:
    try:
        return INSTALL_STATUS.get(int(status), "not installed")
    except (TypeError, ValueError):
        return "not installed"


class GeelarkAppClient:
    PAGE_SIZE = 100

    def __init__(self, transport: GeelarkTransport | None = None) -> None:
        self.transport = transport or GeelarkTransport()

    def installed_apps(self, profile_id: str) -> list[dict]:
        """Apps already on this phone."""
        return self.transport.paged(INSTALLED_PATH, page_size=self.PAGE_SIZE,
                                    extra={"envId": profile_id})

    # Kept as the old name so existing callers keep working; it reads the
    # installed list, which is what they wanted.
    list_apps = installed_apps

    def installable_apps(self, profile_id: str, name: str = "") -> list[dict]:
        """The catalogue of apps that could be installed on this phone."""
        extra: dict[str, object] = {"envId": profile_id}
        if name:
            extra["name"] = name
        return self.transport.paged(INSTALLABLE_PATH, page_size=self.PAGE_SIZE,
                                    extra=extra)

    def find_app(self, profile_id: str, package_name: str) -> list[dict]:
        """Installed rows matching a package name.

        Returns a list because Geelark commonly carries several versions of the
        same package, and which one is installed matters for automation.
        """
        return [app for app in self.installed_apps(profile_id)
                if app.get("packageName") == package_name]

    def installable_versions(self, profile_id: str, package_name: str) -> list[dict]:
        """Every installable version of one package, flattened.

        The catalogue nests versions under `appVersionInfoList`, so the
        `appVersionId` that `/app/install` wants is not on the app row itself --
        reading the outer `id` and passing that installs nothing.
        """
        versions: list[dict] = []
        for app in self.installable_apps(profile_id):
            if app.get("packageName") != package_name:
                continue
            for version in app.get("appVersionInfoList") or []:
                versions.append({
                    "app_id": str(app.get("id") or ""),
                    "app_name": str(app.get("appName") or ""),
                    "package_name": str(app.get("packageName") or ""),
                    "app_version_id": str(version.get("id") or ""),
                    "version_name": str(version.get("versionName") or ""),
                    "version_code": str(version.get("versionCode") or ""),
                    "install_status": install_status_label(version.get("installStatus")),
                })
        return versions

    def install_app(self, profile_id: str, app_version_id: str) -> dict:
        """Install one version. The phone must be running (else 42002)."""
        return self.transport.post(INSTALL_PATH, {
            "envId": profile_id,
            "appVersionId": app_version_id,
        })

    def request_install(self, profile_id: str, app_version_id: str) -> str:
        """Ask for an install, tolerating one that is already under way.

        Returns "requested" or "already-installing".

        A newly created phone starts installing the team's apps by itself, so
        asking for Instagram on a fresh phone frequently answers
        ``42003 app installing``. That is not a failure -- it is the thing you
        wanted, already happening -- but it arrives as an error and reads like
        one. Treating it as fatal aborts a migration run on phones that were
        about to be perfectly fine.
        """
        try:
            self.install_app(profile_id, app_version_id)
        except GeelarkError as error:
            if error.code != CODE_APP_INSTALLING:
                raise
            return "already-installing"
        return "requested"

    def uninstall_app(self, profile_id: str, package_name: str) -> dict:
        """Uninstall by **package name** -- this endpoint does not take a
        version id, despite install taking one."""
        return self.transport.post(UNINSTALL_PATH, {
            "envId": profile_id,
            "packageName": package_name,
        })

    def start_app(self, profile_id: str, package_name: str) -> dict:
        return self.transport.post(START_PATH, {
            "envId": profile_id,
            "packageName": package_name,
        })

    def stop_app(self, profile_id: str, package_name: str) -> dict:
        return self.transport.post(STOP_PATH, {
            "envId": profile_id,
            "packageName": package_name,
        })
