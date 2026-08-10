"""A model that exists in one system and not another must be a finding.

The bug these cover: two models (Kathi, Katherine) were created in MultiLogin,
cloned onto 15 real phones and warmed up for six days while every posting code
path was blind to them -- their Airtable rows were still named `Blank (N)`, so
the "first word of the profile name is the model" join key pooled them with
staging. Nothing anywhere said so. Zero variants is indistinguishable from a
quiet day unless something compares the systems on purpose.
"""

from unittest import TestCase

from adb_bot.automation import model_inventory
from adb_bot.automation.model_inventory import INFO, WARN, Inventory, diff_models


def _kinds(findings, kind):
    return [f for f in findings if f.kind == kind]


class HealthyInventoryTest(TestCase):
    """The check has to be quiet when everything lines up, or it is noise."""

    def test_a_fully_onboarded_model_produces_nothing(self):
        inv = Inventory(
            raw_folders=["Jasmin"],
            mlx_folders={"Jasmin": ["Jasmin 1", "Jasmin 2"]},
            target_keys={"jasmin": 2},
            model_rows={"jasmin"},
        )
        self.assertEqual(diff_models(inv), [])

    def test_staging_folders_are_not_models(self):
        # "Default folder" holds the unnamed Blank profiles; flagging it would
        # fire on every run forever.
        inv = Inventory(mlx_folders={"Default folder": ["Blank (1)", "Blank (2)"],
                                     "Caio tests": ["Rodrigo 1"]})
        self.assertEqual(diff_models(inv), [])

    def test_the_alias_pair_stays_silent(self):
        # 01_Raw_Videos/Corina holds Nikki's clips and Mandy's holds Luisa's.
        # Reading the folder name literally would report two missing models and
        # two content-less ones -- four false findings on a healthy fleet.
        inv = Inventory(
            raw_folders=["Corina", "Mandy"],
            mlx_folders={"NIkki": ["Nikki 1"], "Luisa": ["Luisa 1"]},
            target_keys={"nikki": 19, "luisa": 11},
            model_rows={"nikki", "luisa"},
            aliases={"corina": "Nikki", "mandy": "Luisa"},
        )
        self.assertEqual(diff_models(inv), [])


class NewModelIsVisibleTest(TestCase):
    def test_mlx_folder_whose_profiles_are_still_named_blank_is_flagged(self):
        # The earliest possible signal, and the one that fires days before any
        # clip is uploaded: the folder exists, the profiles in it do not carry
        # its name yet.
        inv = Inventory(mlx_folders={"Kathi": [f"Blank ({n})" for n in range(1, 11)]})
        found = _kinds(diff_models(inv), "mlx_folder_unnamed")
        self.assertEqual([f.model for f in found], ["Kathi"])
        self.assertEqual(found[0].severity, WARN)
        self.assertIn("10 profile(s)", found[0].detail)

    def test_the_hint_names_the_write_that_is_missing(self):
        # mlx-sync writes Profile Name only on create, so renaming in MultiLogin
        # alone never reaches Airtable. If the hint does not say that, the
        # operator does the obvious thing and the model stays invisible.
        inv = Inventory(mlx_folders={"Kathi": ["Blank (1)"]})
        hint = _kinds(diff_models(inv), "mlx_folder_unnamed")[0].hint
        self.assertIn("MultiLogin AND", hint)
        self.assertIn("Profiles (Cloning)", hint)

    def test_folder_case_mismatch_is_not_a_finding(self):
        # The live workspace spells one folder "NIkki" while its profiles are
        # "Nikki 1".
        inv = Inventory(mlx_folders={"NIkki": ["Nikki 1", "Nikki 2"]},
                        raw_folders=["Nikki"], target_keys={"nikki": 2},
                        model_rows={"nikki"})
        self.assertEqual(_kinds(diff_models(inv), "mlx_folder_unnamed"), [])

    def test_a_renamed_folder_clears_the_finding(self):
        inv = Inventory(mlx_folders={"Kathi": ["Kathi 1", "Blank (2)"]})
        self.assertEqual(_kinds(diff_models(inv), "mlx_folder_unnamed"), [])


class RawFolderTest(TestCase):
    def test_an_empty_raw_folder_with_no_profiles_is_still_reported(self):
        # This is the whole point of `list_folder_names`: an empty folder is what
        # a new model's Drive folder looks like on day one, and `list_by_model`
        # drops it, so nothing downstream ever saw it.
        inv = Inventory(raw_folders=["Katherine"])
        found = _kinds(diff_models(inv), "raw_folder_no_targets")
        self.assertEqual([f.model for f in found], ["Katherine"])

    def test_the_hint_offers_the_alias_setting_rather_than_a_code_edit(self):
        inv = Inventory(raw_folders=["Katherine"])
        hint = _kinds(diff_models(inv), "raw_folder_no_targets")[0].hint
        self.assertIn("RAW_FOLDER_MODEL_ALIASES", hint)

    def test_targets_with_no_raw_folder_are_reported(self):
        inv = Inventory(target_keys={"lou": 4}, model_rows={"lou"}, raw_folders=[])
        found = _kinds(diff_models(inv), "targets_no_raw_folder")
        self.assertEqual([f.model for f in found], ["lou"])

    def test_blank_staging_targets_are_not_a_missing_model(self):
        # 67 active "Blank (N)" staging profiles are a known condition, not news;
        # reporting them would bury the two findings that matter.
        inv = Inventory(target_keys={"blank": 67})
        self.assertEqual(_kinds(diff_models(inv), "targets_no_raw_folder"), [])


class ModelsRowTest(TestCase):
    def test_a_model_with_no_models_row_is_information_not_a_gap(self):
        # Posting works without one (queue_runner treats a missing schedule as
        # flexible mode), so this must not be able to fail the check on its own.
        inv = Inventory(raw_folders=["Nikki"], target_keys={"nikki": 19},
                        mlx_folders={"Nikki": ["Nikki 1"]}, model_rows=set())
        found = _kinds(diff_models(inv), "no_models_row")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, INFO)

    def test_it_is_reported_once_per_model_not_once_per_system(self):
        inv = Inventory(raw_folders=["Kathi"], mlx_folders={"Kathi": ["Kathi 1"]},
                        target_keys={"kathi": 1}, model_rows=set())
        self.assertEqual(len(_kinds(diff_models(inv), "no_models_row")), 1)


class CollectTest(TestCase):
    class _Airtable:
        def profile_targets_by_model(self):
            return {"jasmin": [{"handle": "j1"}, {"handle": "j2"}]}

        def models_by_name(self):
            return {"jasmin": "recM1"}

    class _Source:
        def list_folder_names(self):
            return ["Jasmin", "Kathi"]

    class _OldSource:
        """A raw source predating list_folder_names."""

        def list_by_model(self):
            return {"Jasmin": []}

    def test_collect_groups_mlx_profiles_by_folder_name(self):
        inv = model_inventory.collect(
            airtable=self._Airtable(),
            mlx_profiles=[{"folder_id": "f1", "serial_name": "Blank (1)"},
                          {"folder_id": "f1", "serial_name": "Blank (2)"},
                          {"folder_id": "f2", "serial_name": "Jasmin 1"}],
            mlx_folders=[{"folder_id": "f1", "name": "Kathi"},
                         {"folder_id": "f2", "name": "Jasmin"}],
            raw_source=self._Source(), aliases={})
        self.assertEqual(inv.mlx_folders["Kathi"], ["Blank (1)", "Blank (2)"])
        self.assertEqual(inv.target_keys, {"jasmin": 2})
        self.assertEqual(inv.raw_folders, ["Jasmin", "Kathi"])
        self.assertEqual(inv.model_rows, {"jasmin"})

    def test_a_source_without_list_folder_names_still_works(self):
        inv = model_inventory.collect(raw_source=self._OldSource(), aliases={})
        self.assertEqual(inv.raw_folders, ["Jasmin"])

    def test_missing_clients_do_not_raise(self):
        inv = model_inventory.collect(aliases={})
        self.assertEqual(diff_models(inv), [])


class SummaryTest(TestCase):
    def test_a_clean_inventory_says_so(self):
        self.assertIn("lines up", model_inventory.summarise([]))

    def test_the_summary_names_the_model(self):
        findings = diff_models(Inventory(mlx_folders={"Kathi": ["Blank (1)"]}))
        self.assertIn("Kathi", model_inventory.summarise(findings))
