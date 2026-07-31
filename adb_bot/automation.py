from .core.models import Profile
from .core.logger import get_logger


class AutomationRunner:
    def __init__(self) -> None:
        self.logger = get_logger("automation")
        self.flows = {}

    def register_flow(self, flow) -> None:
        self.flows[flow.name] = flow
        self.logger.info("Registered automation flow: %s", flow.name)

    def run(self, profile: Profile) -> None:
        self.logger.info("Running default automation for profile %s on %s", profile.id, profile.target)

    def run_flow(self, flow_name: str, profile: Profile, adb_client=None, logger=None, should_stop=None):
        flow = self.flows.get(flow_name)
        if not flow:
            raise ValueError(f"Unknown automation flow: {flow_name}")
        self.logger.info("Executing flow '%s' for profile %s", flow_name, profile.id)
        return flow.run(profile, adb_client=adb_client, logger=logger or self.logger, should_stop=should_stop)

    def install_app(self, profile: Profile, apk_path: str) -> None:
        self.logger.info("Install app placeholder for profile %s", profile.id)

    def launch_app(self, profile: Profile, package_name: str) -> None:
        self.logger.info("Launch app placeholder for profile %s", profile.id)

    def perform_actions(self, profile: Profile) -> None:
        self.logger.info("Generic actions placeholder for profile %s", profile.id)
