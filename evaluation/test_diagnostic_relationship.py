import unittest
import json

from src.clinical_validation import assess_diagnostic_relationship, assess_clinical_relationships


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




class PilotRelationshipTests(unittest.TestCase):
    HCR = ('In the ROC analysis, the cut-off value of the HCR score in the group with an ischemic event '
           'was found to be 79% sensitivity and 81% specificity.')
    AUC = ('ROC analysis showed moderate discriminative ability for ELR [area under the curve (AUC)=0.633] '
           'and PLR (AUC=0.627), while Neu% had limited predictive value.')
    LOOP = ('In patients presenting with ischemic stroke without diagnosed AF, an implantable loop recorder '
            '(ie, subcutaneous telemetry device) can evaluate patients for intermittent AF.')
    TREAT = ('The evidence unequivocally supports the fact that revascularization of the culprit lesion '
             'in acute coronary syndromes leads to improved survival rates.')

    def hcr(self, **extra):
        return dict(diagnostic_method='HCR score', clinical_outcome='sensitivity', numeric_value='79%',
                    patient_group='the group with an ischemic event', **extra)

    def test_hcr_separate_metrics(self):
        for metric, value in [('sensitivity', '79%'), ('specificity', '81%')]:
            f = self.hcr()
            f.update(clinical_outcome=metric, numeric_value=value)
            self.assertEqual(assess_diagnostic_relationship(f, self.HCR), 'supported_local')

    def test_hcr_wrong_value_metric_method_or_group(self):
        for extra in [dict(numeric_value='81%'), dict(clinical_outcome='AUC'), dict(diagnostic_method='HbA1c'),
                      dict(patient_group='controls'), dict(numeric_value='99%')]:
            f = self.hcr(); f.update(extra)
            self.assertNotEqual(assess_diagnostic_relationship(f, self.HCR), 'supported_local')

    def test_auc_separate_methods_and_swapped_values(self):
        for method, right, wrong in [('ELR', '0.633', '0.627'), ('PLR', '0.627', '0.633')]:
            f = dict(diagnostic_method=method, clinical_outcome='area under the curve (AUC)',
                     numeric_value=right, effect_measure='AUC')
            self.assertEqual(assess_diagnostic_relationship(f, self.AUC), 'supported_local')
            f['numeric_value'] = wrong
            self.assertEqual(assess_diagnostic_relationship(f, self.AUC), 'contradicted')

    def test_auc_unknown_metric_and_method_lists(self):
        for extra in [dict(diagnostic_method='ELR and PLR'), dict(clinical_outcome='sensitivity'),
                      dict(numeric_value='0.999'), dict(diagnostic_method='ROC analysis'), dict(diagnosis='AF')]:
            f = dict(diagnostic_method='ELR', clinical_outcome='AUC', numeric_value='0.633'); f.update(extra)
            self.assertNotEqual(assess_diagnostic_relationship(f, self.AUC), 'supported_local')

    def test_loop_short_and_parenthetical_methods(self):
        for method in ['implantable loop recorder', 'implantable loop recorder (ie, subcutaneous telemetry device)']:
            f = dict(diagnostic_method=method, diagnosis='intermittent AF',
                     patient_group='patients presenting with ischemic stroke without diagnosed AF')
            self.assertEqual(assess_diagnostic_relationship(f, self.LOOP), 'supported_local')

    def test_loop_population_required_and_not_borrowed(self):
        for group in [None, 'patients with AF', 'ischemic stroke']:
            f = dict(diagnostic_method='implantable loop recorder', diagnosis='intermittent AF', patient_group=group)
            self.assertNotEqual(assess_diagnostic_relationship(f, self.LOOP), 'supported_local')
        f['patient_group'] = 'patients presenting with ischemic stroke without diagnosed AF'
        for q in [self.LOOP.replace('can evaluate', 'cannot evaluate'), self.LOOP.replace('can evaluate', 'may evaluate'),
                  self.LOOP.replace('intermittent AF', 'MI'), self.LOOP.replace(', an', '. An')]:
            self.assertNotEqual(assess_diagnostic_relationship(f, q), 'supported_local')

    def test_treatment_short_and_full_names(self):
        for key in ['procedure', 'drug']:  # native SQL representation uses drug alias
            for method in ['revascularization', 'revascularization of the culprit lesion']:
                f = {key: method, 'patient_group': 'acute coronary syndromes', 'clinical_outcome': 'improved survival rates'}
                self.assertEqual(assess_clinical_relationships(f, self.TREAT), 'supported_local')

    def test_treatment_unrelated_group_benefit_or_negation(self):
        f = dict(procedure='revascularization', patient_group='acute coronary syndromes', clinical_outcome='improved survival rates')
        for q in [self.TREAT.replace('leads to', 'does not lead to'), self.TREAT.replace('leads to', 'may lead to'),
                  self.TREAT.replace('leads to', '. Separately there were'), self.TREAT.replace('acute coronary syndromes', 'stable CAD')]:
            self.assertNotEqual(assess_clinical_relationships(f, q), 'supported_local')
        for extra in [dict(patient_group='all patients'), dict(numeric_value='90%'), dict(clinical_outcome='reduced stroke')]:
            self.assertNotEqual(assess_clinical_relationships({**f, **extra}, self.TREAT), 'supported_local')

    def test_extra_statistics_and_drugs_cannot_be_donated(self):
        for extra in [dict(drug='Apixaban'), dict(dose='5'), dict(p_value='0.05'), dict(confidence_interval='0.5-0.9')]:
            f = self.hcr(); f.update(extra)
            self.assertNotEqual(assess_diagnostic_relationship(f, self.HCR), 'supported_local')

    def test_objectives_staging_and_population_do_not_establish_diagnosis(self):
        for f, q in [
            (dict(diagnostic_method='ECG', diagnosis='AF'), 'AF staging includes ECG and imaging.'),
            (dict(diagnostic_method='delta HS Troponin I (0h-1h)'), 'The delta HS Troponin I (0h-1h) and delta HS Troponin I (0h-3h) values of the patients were calculated as percentage changes.'),
            (dict(diagnostic_method='ultrasound', diagnosis='carotid stenosis'), 'Patients with carotid stenosis underwent ultrasound.'),
            (dict(diagnostic_method='ROC analysis', diagnosis='AF'), 'AF was diagnosed using ROC analysis.')]:
            self.assertNotEqual(assess_diagnostic_relationship(f, q), 'supported_local')

    def test_metric_does_not_cross_sentence_or_uncertainty(self):
        for q in [self.HCR.replace('was found', 'may have been found'), self.HCR.replace('was found', 'was not found'),
                  self.HCR.replace(' was found', '. It was found')]:
            self.assertNotEqual(assess_diagnostic_relationship(self.hcr(), q), 'supported_local')


class PilotSQLBoundaryTests(unittest.TestCase):
    """Entire validation boundary using an in-memory, explicitly fake source."""
    def row(self, f, quote):
        from src.clinical_validation import ValidationContext
        from src.clinical_pipeline import PIPELINE_FIELDS, build_row
        from types import SimpleNamespace
        f = {**dict.fromkeys(PIPELINE_FIELDS), **f, 'evidence_quote': quote}
        ctx = ValidationContext(pages={'mock-doc': {1: quote}}, makale_by_doc={'mock-doc': 'mock-article'},
                                chunks={('mock-doc', 1): [('mock-chunk', quote)]}, chunk_ids={'mock-chunk'},
                                chunk_versions={'mock-chunk': 'mock-active'}, active_versions={'mock-doc': 'mock-active'})
        sources = SimpleNamespace(ctx=ctx, chunk_index={'mock-chunk': 0}, icd_names={})
        item = dict(candidate=dict(aday_id='mock-pilot', document_id='mock-doc', makale_id='mock-article',
                                   sayfa=1, kaynak_turu='metin', cumle=quote), allowed_quote_spans=[quote])
        row, problem = build_row(f, item, sources, model='mock-no-api')
        self.assertIsNone(problem)
        return row, ctx

    def test_each_numeric_method_passes_whole_source_boundary(self):
        from src.clinical_validation import validate_row
        fixtures = [
            (dict(diagnostic_method='HCR score', clinical_outcome=m, numeric_value=v,
                  patient_group='the group with an ischemic event'), PilotRelationshipTests.HCR)
            for m,v in [('sensitivity','79%'), ('specificity','81%')]]
        fixtures += [(dict(diagnostic_method=m, clinical_outcome='area under the curve (AUC)',
                           numeric_value=v, effect_measure='AUC'), PilotRelationshipTests.AUC)
                     for m,v in [('ELR','0.633'), ('PLR','0.627')]]
        for f,q in fixtures:
            with self.subTest(f=f):
                row,ctx=self.row(f,q)
                self.assertEqual(validate_row(row,ctx), (True,None,None))
                self.assertEqual(row['review_status'], 'pending')
                note=json.loads(row['taslak_notu'])
                self.assertEqual(note['clinical_result']['original_text'], f['clinical_outcome'])
                row['deger']='999'
                self.assertFalse(validate_row(row,ctx)[0])

    def test_loop_and_treatment_pass_with_source_scope(self):
        from src.clinical_validation import validate_row
        for f,q in [
            (dict(diagnostic_method='implantable loop recorder (ie, subcutaneous telemetry device)',
                  diagnosis='intermittent AF',patient_group='patients presenting with ischemic stroke without diagnosed AF'),
             PilotRelationshipTests.LOOP),
            (dict(procedure='revascularization',patient_group='acute coronary syndromes',
                  clinical_outcome='improved survival rates'),PilotRelationshipTests.TREAT)]:
            row,ctx=self.row(f,q)
            self.assertEqual(validate_row(row,ctx),(True,None,None))
            row['hasta_grubu']='all patients'
            self.assertFalse(validate_row(row,ctx)[0])

    def test_stored_result_cannot_hide_conflicting_metric(self):
        from src.clinical_validation import validate_row
        row,ctx=self.row(dict(diagnostic_method='ELR',clinical_outcome='area under the curve (AUC)',
                              numeric_value='0.633',effect_measure='AUC'),PilotRelationshipTests.AUC)
        note=json.loads(row['taslak_notu']); note['clinical_result']['original_text']='specificity'
        row['taslak_notu']=json.dumps(note)
        self.assertFalse(validate_row(row,ctx)[0])
        note['clinical_result']['original_text']='area under the curve (AUC)'
        note['extracted_fields']['numeric_value']='0.627'
        row['taslak_notu']=json.dumps(note)
        self.assertFalse(validate_row(row,ctx)[0])

    def test_statistical_method_cannot_bypass_direct_boundary(self):
        from src.clinical_validation import validate_row
        row,ctx=self.row(dict(diagnostic_method='ROC analysis',diagnosis='AF'), 'AF was diagnosed using ROC analysis.')
        self.assertFalse(validate_row(row,ctx)[0])

    def test_numeric_types_and_compound_hcr_method_rejected(self):
        for f,q in [
            (dict(diagnostic_method='HCR score',clinical_outcome='sensitivity',numeric_value=79,
                  patient_group='the group with an ischemic event'),PilotRelationshipTests.HCR),
            (dict(diagnostic_method='HCR and ELR',clinical_outcome='sensitivity',numeric_value='79%',
                  patient_group='the group with an ischemic event'),PilotRelationshipTests.HCR.replace('HCR score','HCR and ELR'))]:
            self.assertNotEqual(assess_diagnostic_relationship(f,q),'supported_local')

    def test_real_carotid_population_definition_is_not_a_treatment_result(self):
        from src.clinical_validation import validate_row
        q = ('In this retrospective case-control study, 189 patients were included: '
             '96 with \u2265 70% CAS who underwent carotid endarterectomy and 93 controls '
             'without carotid plaques or increased intima-media thickness.')
        f = dict(procedure='carotid endarterectomy',patient_group='96 with \u2265 70% CAS',
                 comparison='93 controls without carotid plaques or increased intima-media thickness',
                 sample_size='189 patients')
        # Use a literal source character, not an artificial escape in the quote.
        q=q.replace('\\u2265','\u2265'); f['patient_group']=f['patient_group'].replace('\\u2265','\u2265')
        self.assertEqual(assess_clinical_relationships(f,q),'uncertain')
        row,ctx=self.row(f,q)
        self.assertFalse(validate_row(row,ctx)[0])

if __name__ == "__main__":
    unittest.main()
