import unittest

from adb_bot.automation.flows.instagram import InstagramScrollFlow, InstagramWarmUpDay1Flow


class WarmUpReturnHomeTest(unittest.TestCase):
    def test_warmup_flow_can_build_return_home_command_without_crashing(self):
        flow = InstagramWarmUpDay1Flow()
        command = flow._build_return_home_command("target-device")

        self.assertIsInstance(command, str)
        self.assertIn("adb -s target-device shell", command)

    def test_progress_steps_are_based_on_flow_plan_size(self):
        scroll_flow = InstagramScrollFlow()
        warmup_flow = InstagramWarmUpDay1Flow()

        scroll_total = scroll_flow.get_progress_total_steps("target-device")
        warmup_total = warmup_flow.get_progress_total_steps("target-device")

        self.assertGreater(scroll_total, 10)
        self.assertGreater(warmup_total, 20)


if __name__ == "__main__":
    unittest.main()
