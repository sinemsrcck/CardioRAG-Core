import unittest

from src.clinical_validation import assess_diagnostic_relationship


class DiagnosticRelationshipTests(unittest.TestCase):
    def check(self, evidence, expected, **extra):
        fields = {
            "diagnosis": "AMI",
            "diagnostic_method": "HS troponin-I",
            **extra,
        }
        self.assertEqual(
            assess_diagnostic_relationship(fields, evidence),
            expected,
            msg=evidence,
        )

    def test_explicit_relationships(self):
        for evidence in (
            "AMI was diagnosed using HS troponin-I.",
            "AMI was detected with HS troponin-I.",
            "HS troponin-I detected AMI.",
            "HS troponin-I was used to diagnose AMI.",
            "Sensitivity of HS troponin-I for detecting AMI.",
            "Specificity of HS troponin-I for diagnosing AMI.",
        ):
            with self.subTest(evidence=evidence):
                self.check(evidence, "supported_local")

    def test_uncertain_relationships(self):
        for evidence in (
            "HS troponin-I was evaluated in patients with AMI.",
            "HS troponin-I was not used to diagnose AMI.",
            "HS troponin-I may detect AMI.",
            "HS troponin-I and AMI were studied.",
            "HS troponin-I failed to detect AMI.",
        ):
            with self.subTest(evidence=evidence):
                self.check(evidence, "uncertain")

    def test_missing_evidence(self):
        self.check(
            "AMI was diagnosed using ECG.",
            "contradicted",
        )

    def test_numeric_fields_remain_restricted(self):
        self.check(
            "Sensitivity of HS troponin-I for detecting AMI.",
            "uncertain",
            numeric_value="90",
        )


    AF_QUOTE = (
        "AF can be detected incidentally during clinical encounters, with wearable devices, "
        "or through interrogation of cardiac implanted electronic devices."
    )

    def af(self, method, evidence=None, **extra):
        return assess_diagnostic_relationship(
            {"diagnosis": "AF", "diagnostic_method": method, **extra},
            self.AF_QUOTE if evidence is None else evidence)

    def test_p01_alternatives_are_separate_named_methods(self):
        for method in ("wearable devices", "interrogation of cardiac implanted electronic devices"):
            with self.subTest(method=method):
                self.assertEqual(self.af(method), "supported_local")

    def test_clinical_encounters_is_not_a_named_test(self):
        for method in ("clinical encounters", "clinical encounter"):
            with self.subTest(method=method):
                self.assertEqual(self.af(method), "uncertain")

    def test_combined_p01_method_field_stays_uncertain(self):
        for method in (
            "wearable devices, or through interrogation of cardiac implanted electronic devices",
            "wearable devices and interrogation of cardiac implanted electronic devices",
        ):
            with self.subTest(method=method):
                self.assertEqual(self.af(method), "uncertain")

    def test_legacy_explicit_disease_head_is_preserved(self):
        for evidence in (
            "AF can be detected with wearable devices.",
            "AF was diagnosed using wearable devices.",
            "AF can be detected incidentally with wearable devices.",
            "AF can be detected with ECG, or through interrogation of cardiac implanted electronic devices.",
        ):
            method = ("interrogation of cardiac implanted electronic devices"
                      if "interrogation" in evidence else "wearable devices")
            with self.subTest(evidence=evidence):
                self.assertEqual(self.af(method, evidence), "supported_local")

    def test_no_method_from_another_sentence_or_disease(self):
        for evidence in (
            "AF was discussed. MI can be detected with wearable devices.",
            "AF can be detected with ECG. MI can be detected with wearable devices.",
            "AF was discussed; MI can be detected with wearable devices.",
            "AF can be detected with ECG, while MI was diagnosed with wearable devices.",
            "AF can be detected with ECG, or MI can be detected with wearable devices.",
        ):
            with self.subTest(evidence=evidence):
                self.assertNotEqual(self.af("wearable devices", evidence), "supported_local")

    def test_negated_and_uncertain_alternative_lists_are_blocked(self):
        for prefix in (
            "AF cannot be detected", "AF may be detected", "AF might be detected",
            "AF was not detected", "AF was never detected", "AF was possibly detected",
        ):
            evidence = prefix + " with wearable devices, or through interrogation of cardiac implanted electronic devices."
            for method in ("wearable devices", "interrogation of cardiac implanted electronic devices"):
                with self.subTest(evidence=evidence, method=method):
                    self.assertNotEqual(self.af(method, evidence), "supported_local")

    def test_cooccurrence_does_not_establish_alternative_relationship(self):
        evidence = "AF was evaluated in patients with wearable devices, or through interrogation of cardiac implanted electronic devices."
        self.assertEqual(self.af("wearable devices", evidence), "uncertain")

    def test_malformed_or_extra_clause_in_list_stays_uncertain(self):
        for evidence in (
            "AF can be detected with ECG for MI, or with wearable devices.",
            "AF can be detected with wearable devices, or mortality was reduced.",
            "AF can be detected with ECG, unrelated wearable devices.",
        ):
            with self.subTest(evidence=evidence):
                self.assertNotEqual(self.af("wearable devices", evidence), "supported_local")

    def test_p01_cannot_donate_unrelated_drug_or_dose(self):
        evidence = self.AF_QUOTE + " Apixaban 5 mg was administered."
        for extra in ({"drug": "Apixaban"}, {"dose": "5", "dose_unit": "mg"}):
            with self.subTest(extra=extra):
                self.assertEqual(self.af("wearable devices", evidence, **extra), "contradicted")

    def test_p01_numeric_and_outcome_fields_stay_review_only(self):
        for field, value in (
            ("numeric_value", "90"), ("value_unit", "%"), ("patient_group", "adults"),
            ("comparison", "ECG"), ("clinical_outcome", "mortality"),
        ):
            with self.subTest(field=field):
                self.assertEqual(self.af("wearable devices", **{field: value}), "uncertain")

    def test_alternative_names_must_match_full_method_and_disease(self):
        self.assertEqual(self.af("12-lead ECG"), "contradicted")
        self.assertEqual(assess_diagnostic_relationship(
            {"diagnosis": "MI", "diagnostic_method": "wearable devices"}, self.AF_QUOTE),
            "contradicted")



    def test_symbol_separated_methods_remain_review_only(self):
        for method in ("ECG / wearable devices", "ECG + wearable devices", "ECG & wearable devices"):
            with self.subTest(method=method):
                self.assertEqual(self.af(method, "AF was detected with " + method + "."), "uncertain")

    def test_conflicting_or_uncertain_same_pair_cannot_be_overridden(self):
        for first in (
            "AF was not detected with wearable devices.",
            "AF may be detected with wearable devices.",
        ):
            evidence = first + " AF was detected with wearable devices."
            with self.subTest(evidence=evidence):
                self.assertEqual(self.af("wearable devices", evidence), "uncertain")



if __name__ == "__main__":
    unittest.main()
