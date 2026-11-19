"""Offline evidence tests. No SQL, models, API or expert approval."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.claim_validator import validate_claims, parse_claim, SCOPE


ELR = "ELR had an AUC of 0.633 (95% CI: 0.562-0.705, p<0.001)."
PLR = "PLR had an AUC of 0.627 (95% CI: 0.556-0.698, p=0.002)."


def source(text, number=1):
    return {"number": number, "chunk_id": "mock:" + str(number), "document_id": "mock-doc",
        "filename": "mock.pdf", "page_number": 5, "processing_version": "mock-active-v1",
        "source_text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}


class DeterministicClaimTests(unittest.TestCase):
    def check(self, claim, text=ELR, passed=True):
        report = validate_claims(claim, [source(text)])
        self.assertEqual(report["claims_verified"], passed, report)
        self.assertFalse(report["scientific_verification"])
        self.assertFalse(report["expert_approved"])
        self.assertEqual(report["scope"], SCOPE)
        return report

    def test_correct_auc_p_and_ci(self):
        self.check("ELR AUC=0.633, 95% CI: 0.562-0.705, p<0.001 [1].")

    def test_incorrect_auc(self):
        self.check("ELR AUC=0.627 [1].", passed=False)

    def test_incorrect_p_comparator(self):
        self.check("ELR p=0.001 [1].", passed=False)

    def test_incorrect_ci(self):
        self.check("ELR 95% CI: 0.556-0.698 [1].", passed=False)

    def test_numbers_cannot_migrate_between_biomarkers(self):
        self.check("ELR AUC=0.627, p=0.002 [1].", ELR + " " + PLR, passed=False)

    def test_correct_plr_in_mixed_chunk(self):
        self.check("PLR AUC=0.627, p=0.002 [1].", ELR + " " + PLR)

    def test_same_number_with_wrong_statistic_label(self):
        self.check("ELR specificity=80% [1].", "ELR sensitivity=80%.", passed=False)

    def test_patient_and_control_counts(self):
        text = "Study group included 96 patients and control group included 93 patients."
        self.check("Study group n=96 and control group n=93 [1].", text)

    def test_swapped_patient_and_control_counts(self):
        text = "Study group included 96 patients and control group included 93 patients."
        self.check("Study group n=93 and control group n=96 [1].", text, passed=False)

    def test_wrong_patient_population(self):
        self.check("Without diabetes AF patients ELR AUC=0.633 [1].",
                   "Among diabetic AF patients, ELR had an AUC of 0.633.", passed=False)

    def test_missing_restrictive_population_is_not_generalized(self):
        self.check("ELR AUC=0.633 [1].", "Among diabetic AF patients, ELR AUC=0.633.", passed=False)

    def test_bilingual_population_and_statistics(self):
        self.check("Diyabetli AF hastalarında ELR için AUC=0,633 [1].",
                   "Among diabetic AF patients, ELR had an AUC of 0.633.")

    def test_correct_measurement_interval(self):
        self.check("Delta hs troponin I (1-3h) AUC=0.838, p<0.001 [1].",
                   "Delta hs troponin I (1-3h) AUC=0.838, p<0.001.")

    def test_wrong_measurement_interval(self):
        self.check("Delta hs troponin I (0-1h) AUC=0.838 [1].",
                   "Delta hs troponin I (1-3h) AUC=0.838.", passed=False)

    def test_turkish_measurement_interval(self):
        self.check("Delta yüksek duyarlılıklı troponin I 1–3 saat AUC=0,838 [1].",
                   "Delta hs troponin I 1-3 hours AUC=0.838.")

    def test_wrong_clinical_endpoint(self):
        self.check("ELR AMI AUC=0.633 [1].", "ELR ischemic stroke AUC=0.633.", passed=False)

    def test_secondary_study_is_not_an_original_result(self):
        self.check("Bu çalışmada ELR AUC=0.633 [1].",
                   "Keller et al. reported ELR AUC=0.633.", passed=False)

    def test_explicit_original_result(self):
        self.check("Bu çalışmada ELR AUC=0.633 [1].", "In this study, ELR AUC=0.633.")

    def test_guideline_is_not_original_research(self):
        self.check("This study found FFR-CT useful [1].",
                   "The guideline classifies FFR-CT as a class IIa recommendation.", passed=False)

    def test_guideline_literal_keeps_its_origin(self):
        text = "The guideline classifies FFR-CT as a class IIa recommendation."
        report = self.check(text + " [1]", text)
        self.assertEqual(report["claims"][0]["matches"][0]["source_origin"], "guideline")

    def test_turkish_auc_ci_and_p(self):
        self.check("ELR için AUC=0,633 (%95 GA: 0,562–0,705; p<0,001) [1].")

    def test_malformed_publication_p_is_not_repaired(self):
        self.check("HbA1c p<0.001 [1].", "HbA1c p<001.", passed=False)

    def test_malformed_p_does_not_pass_even_as_literal_copy(self):
        self.check("HbA1c p<001 [1].", "HbA1c p<001.", passed=False)

    def test_unparsed_source_population_cannot_be_generalized(self):
        self.check("ELR AUC=0.633 [1].", "In hemodialysis patients, ELR AUC=0.633.", passed=False)

    def test_absent_disease_group_does_not_support_present_group(self):
        self.check("AMI patients ELR AUC=0.633 [1].", "Patients without AMI ELR AUC=0.633.", passed=False)

    def test_standard_error_is_not_ci(self):
        self.check("Hs troponin I 95% CI: 0.772-0.904 [1].",
                   "Hs troponin I AUC=0.838 ±0.066.", passed=False)

    def test_correct_standard_error(self):
        fields = parse_claim("Hs troponin I AUC=0.838 ±0.066 (SE)")
        self.assertEqual(fields["metrics"]["standard_error"], ["0.066"])
        self.assertNotIn("ci95", fields["metrics"])

    def test_untyped_plus_minus_is_not_assumed_to_be_se(self):
        fields = parse_claim("Hs troponin I AUC=0.838 ±0.066")
        self.assertNotIn("standard_error", fields["metrics"])
        self.assertTrue(fields["ambiguous"])

    def test_auc_percentage_cannot_replace_a_dimensionless_auc(self):
        self.check("ELR AUC=0.633% [1].", passed=False)

    def test_fractional_patient_count_is_uncertain(self):
        self.check("Study group n=96.5 [1].", "Study group n=96.5.", passed=False)

    def test_claim_uses_only_its_own_citations(self):
        report = validate_claims("ELR AUC=0.633 [2].", [source(ELR), source(PLR, 2)])
        self.assertFalse(report["claims_verified"])

    def test_multiple_citations_with_matching_evidence(self):
        report = validate_claims("ELR AUC=0.633 [1,2].", [source(ELR), source(ELR, 2)])
        self.assertTrue(report["claims_verified"])
        self.assertEqual({m["source_number"] for m in report["claims"][0]["matches"]}, {1, 2})

    def test_cross_chunk_statistics_cannot_be_spliced(self):
        report = validate_claims("ELR AUC=0.633, p<0.001 [1,2].",
                                 [source("ELR AUC=0.633."), source("ELR p<0.001.", 2)])
        self.assertFalse(report["claims_verified"])

    def test_conflicting_cited_statistics_abstain(self):
        report = validate_claims("ELR AUC=0.633 [1,2].",
                                 [source(ELR), source("ELR AUC=0.900.", 2)])
        self.assertFalse(report["claims_verified"])

    def test_ambiguous_new_benefit_is_not_hidden_behind_a_correct_number(self):
        self.check("ELR AUC=0.633 and eliminates mortality [1].", passed=False)

    def test_negated_source_does_not_support_positive_claim(self):
        self.check("ELR AUC=0.633 [1].", "ELR AUC was not 0.633.", passed=False)

    def test_complex_tables_remain_uncertain(self):
        self.check("ELR AUC=0.633 [1].", "|Marker|AUC|\n|---|---|\n|ELR|0.633|", passed=False)

    def test_tampered_source_hash_is_rejected(self):
        s = source(ELR); s["source_text"] = PLR
        self.assertFalse(validate_claims("PLR AUC=0.627 [1].", [s])["claims_verified"])

    def test_pending_structured_finding_is_not_ground_truth(self):
        s = {**source(ELR), "review_status": "pending"}
        self.assertFalse(validate_claims("ELR AUC=0.633 [1].", [s])["claims_verified"])

    def test_unknown_paraphrase_is_uncertain(self):
        report = self.check("This is a revolutionary clinical breakthrough [1].", passed=False)
        self.assertEqual(report["claims"][0]["status"], "uncertain")

    def test_audit_quotes_are_real_and_report_is_json_serializable(self):
        report = self.check("ELR AUC=0.633 [1].")
        self.assertIn(report["claims"][0]["matches"][0]["evidence_quote"], ELR)
        json.dumps(report, allow_nan=False)


class ClaimGateIntegrationTests(unittest.TestCase):
    TEXT = "Among diabetic AF patients, ELR ischemic stroke AUC=0.633 (95% CI: 0.562-0.705, p<0.001)."
    ANSWER = "Diabetic AF patients ELR ischemic stroke AUC=0.633 (95% CI: 0.562-0.705, p<0.001) [1]."

    def setUp(self):
        self.chunk = {**source(self.TEXT), "text": self.TEXT}
        self.generate = self.enterContext(patch("src.rag.generate_answer", return_value=self.ANSWER))
        self.enterContext(patch("src.rag.hybrid_search", side_effect=lambda question, chunks, **kw:
                               [{**c, "rrf_score": .02} for c in chunks]))
        self.enterContext(patch("src.rag.rerank", side_effect=lambda question, chunks, **kw:
                               [{**c, "rerank_score": -1.0} for c in chunks]))
        self.enterContext(patch("src.rag.get_llm_client", side_effect=AssertionError("No real API")))
        self.enterContext(patch("src.database.get_connection", side_effect=AssertionError("No real SQL")))
        self.verify = self.enterContext(patch("src.citation_validator.verify_claim", side_effect=AssertionError("No external verifier")))

    def run_rag(self, **kwargs):
        from src.rag import answer_question
        return answer_question("Atriyal fibrilasyonun inme riski nedir?", [self.chunk],
                               embedding_model="minilm", reranker_model="minilm", **kwargs)

    def test_scoped_positive_gate_and_real_evidence_report(self):
        report = self.run_rag()
        self.assertEqual(report["status"], "generated")
        self.assertTrue(report["claims_verified"])
        self.assertFalse(report["citation_check"]["claims_verified"])
        self.assertFalse(report["claim_verification"]["scientific_verification"])
        self.assertIn(report["claim_verification"]["claims"][0]["matches"][0]["evidence_quote"], self.TEXT)
        self.verify.assert_not_called()
        self.generate.assert_called_once()

    def test_unsupported_answer_is_hidden(self):
        self.generate.return_value = self.ANSWER.replace("0.633", "0.999")
        result = self.run_rag()
        self.assertEqual(result["status"], "claim_validation_failed")
        self.assertIsNone(result["answer"])
        self.assertEqual(result["cited_sources"], [])
        self.assertNotIn("claim_text", result["claim_verification"]["claims"][0])

    def test_uncertain_answer_is_hidden(self):
        self.generate.return_value = "A novel treatment eliminates all cardiovascular mortality [1]."
        result = self.run_rag()
        self.assertEqual(result["status"], "claim_validation_failed")
        self.assertIsNone(result["answer"])

    def test_citation_gate_runs_before_claim_gate(self):
        self.generate.return_value = "Claim [99]."
        with patch("src.rag.validate_claims", side_effect=AssertionError("Must not run")) as validate:
            result = self.run_rag()
        self.assertEqual(result["status"], "citation_validation_failed")
        validate.assert_not_called()

    def test_retrieval_only_does_not_run_claim_gate(self):
        with patch("src.rag.validate_claims", side_effect=AssertionError("Must not run")) as validate:
            result = self.run_rag(use_llm=False)
        self.assertEqual(result["status"], "retrieval_only")
        self.assertEqual(result["claim_verification"]["status"], "not_performed")
        self.generate.assert_not_called()
        validate.assert_not_called()

    def test_insufficient_evidence_does_not_run_claim_gate(self):
        self.chunk["text"] = "short"
        with patch("src.rag.validate_claims", side_effect=AssertionError("Must not run")) as validate:
            result = self.run_rag()
        self.assertEqual(result["status"], "insufficient_evidence")
        self.generate.assert_not_called()
        validate.assert_not_called()

    def test_internal_validator_error_fails_closed_without_raw_error(self):
        with patch("src.rag.validate_claims", side_effect=RuntimeError("private-secret")):
            result = self.run_rag()
        self.assertEqual(result["status"], "claim_validation_failed")
        self.assertIsNone(result["answer"])
        self.assertNotIn("private-secret", repr(result))

    def test_archive_keeps_scoped_claim_report(self):
        from src.database import archive_rag_result
        result = self.run_rag()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mock-only.json"
            archive_rag_result(result, path)
            loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["claim_verification"], result["claim_verification"])
        self.assertTrue(loaded["claims_verified"])
        self.assertFalse(loaded["claim_verification"]["expert_approved"])

    def test_mock_sql_logging_keeps_hidden_answer_and_scoped_failed_report(self):
        from src.database import save_rag_result
        from src.clinical_extraction import prepare_rag_save_kwargs
        self.generate.return_value = self.ANSWER.replace("0.633", "0.999")
        result = self.run_rag()
        prepared = prepare_rag_save_kwargs(result)
        conn = Mock(autocommit=False, in_transaction=False)
        cur = conn.cursor.return_value; cur.fetchone.return_value = None
        with patch("src.database._existing_ids", return_value={self.chunk["chunk_id"]}), \
             patch("src.database._validate_rag_sources"):
            qid = save_rag_result(**prepared, connection=conn)
        query = next(call.args[1] for call in cur.execute.call_args_list if "INSERT INTO rag_sorgulari" in call.args[0])
        self.assertEqual(qid, result["query_id"])
        self.assertIsNone(query["answer"])
        self.assertEqual(query["llm_model"], result["llm_model"])
        saved = json.loads(query["citation_check"])
        self.assertEqual(saved["generation_status"], "claim_validation_failed")
        self.assertEqual(saved["claim_verification"], result["claim_verification"])
        self.assertEqual(result["citation_check"]["scope"], "citation_syntax_only")
        conn.commit.assert_not_called()


class RealAFPilotProseRegressionTests(unittest.TestCase):
    """Offline replay of real wording; fixtures are not human-approved labels."""
    ANSWER = 'Atriyal fibrilasyon (AF) \u015fu y\xf6ntemlerle tespit edilebilir:\n\n- **Elektrokardiyografi (EKG) izlemi:** EKG izlemi uzad\u0131k\xe7a AF saptanma olas\u0131l\u0131\u011f\u0131 artar. [1]\n- **Giyilebilir cihazlar:** Kalp ritmini de\u011ferlendiren ak\u0131ll\u0131 saatler AF uyar\u0131s\u0131 verebilir; ancak uyar\u0131n\u0131n EKG ile do\u011frulanmas\u0131 gerekebilir. Apple Heart Study\u2019de AF bildirimi alanlar\u0131n %34\u2019\xfcne sonradan EKG yamas\u0131yla AF tan\u0131s\u0131 konmu\u015ftur. [1]\n- **\u0130mplante edilebilir kalp cihazlar\u0131n\u0131n sorgulanmas\u0131:** Bu cihazlar\u0131n kay\u0131tlar\u0131 incelenerek AF saptanabilir. [2]\n- **\u0130mplante edilebilir loop recorder (ILR):** Deri alt\u0131na yerle\u015ftirilen ve ritmi s\xfcrekli izleyebilen bu cihaz, \xf6zellikle aral\u0131kl\u0131 AF\u2019nin ara\u015ft\u0131r\u0131lmas\u0131nda kullan\u0131labilir. [1, 2]\n- **Klinik ba\u015fvuru s\u0131ras\u0131nda tesad\xfcfi saptama:** AF, belirti vermese bile klinik de\u011ferlendirme s\u0131ras\u0131nda fark edilebilir. [2]\n\nGenel pop\xfclasyonda asemptomatik AF\u2019yi taraman\u0131n yararlar\u0131 konusunda belirsizlik bulunmaktad\u0131r. [1]'
    SOURCE_TEXTS = ['hypertension, obesity, and valvular heart disease (eg, mitral valve stenosis, mitral valve regurgitation), are associated with atrial pathology and AF.<sup>18</sup> \n\n### **AF Screening and Detection** \n\nThe probability of AF detection increases with electrocardiogram (ECG) monitoring duration. The benefits of screening for AF in the general population to detect asymptomatic AF are currently uncertain.<sup>4</sup> An implantable loop recorder (ILR) is a subcutaneous device that can continuously monitor heart rhythm for about 4 years. The LOOP Study (Implantable Loop Recorder Detection of Atrial Fibrillation to Prevent Stroke) randomized 6004 patients aged 70 to 90 years with 1 or more of 4 comorbidities (hypertension, diabetes, prior stroke, or HF) to ILR or usual care.<sup>19</sup> During a median of 64.5 months of follow-up, AF was diagnosed in 31.8% of the ILR group vs 12.2% of the control group. Even though OACs were initiated in patients diagnosed with incident AF, there was no significant difference (HR, 0.80 [95% CI, 0.61\u20131.05]) in risk of the primary outcome of stroke or systemic arterial embolism in patients randomized to ILR (0.88 events per 100 person-years [95% CI, 0.68\u20131.12] vs the control group, 1.09 events [95% CI, 0.96\u20131.24]). \n\nMany individuals use wearable devices that evaluate heart rhythm for AF. In the Apple Heart Study, only 34% of those who received the smartwatch\u2019s notification for AF were subsequently diagnosed with AF using ECG patch', 'asymptomatic. AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices. In patients presenting with ischemic stroke without diagnosed AF, an implantable loop recorder (ie, subcutaneous telemetry device) can evaluate patients for intermittent AF. The 2023 American College of Cardiology (ACC)/American Heart Association (AHA)/American College of Clinical Pharmacy (ACCP)/ Heart Rhythm Society (HRS) Guideline writing group proposed 4 stages of AF evolution: stage 1, at risk, defined as patients with AF-associated risk factors (eg, obesity, hypertension); stage 2, preAF, signs of atrial pathology on electrocardiogram or imaging without AF; stage 3, the presence of paroxysmal (recurrent AF episodes lasting \u22647 days) or persistent (continuous AF episode lasting >7 days) AF subtypes; and stage 4, permanent AF. Lifestyle and risk factor modification, including weight loss and exercise, to prevent AF onset, recurrence, and complications are recommended for all stages. In patients with estimated risk of stroke and thromboembolic events of 2% or greater per year, anticoagulation with a vitamin K antagonist or direct oral anticoagulant reduces stroke risk by 60% to 80% compared with placebo. In most patients, a direct oral anticoagulant, such as apixaban, rivaroxaban, or edoxaban, is recommended over warfarin because of lower bleeding risks. Compared with anticoagulation, aspirin is associated with']

    def sources(self):
        return [source(t, i) for i, t in enumerate(self.SOURCE_TEXTS, 1)]

    def report(self, answer=None, sources=None):
        return validate_claims(answer or self.ANSWER, self.sources() if sources is None else sources)

    def test_eight_real_units_remain_fail_closed(self):
        r = self.report()
        self.assertEqual([c['status'] for c in r['claims']],
            ['uncertain','supported','uncertain','uncertain','supported','uncertain','supported','supported'])
        self.assertFalse(r['claims_verified'])
        self.assertFalse(r['scientific_verification'])
        self.assertFalse(r['expert_approved'])
        for c in r['claims']:
            for m in c['matches']:
                self.assertIn(m['evidence_quote'], self.SOURCE_TEXTS[m['source_number']-1])

    def test_missing_citations_not_repaired(self):
        for i in (0,2): self.assertIn('citation_missing', self.report()['claims'][i]['reasons'])

    def test_unverified_added_confirmation_is_separate(self):
        self.assertIn('additional_confirmation_requirement_unverified', self.report()['claims'][2]['reasons'])

    def test_ilr_restricted_population_not_generalized(self):
        self.assertIn('restricted_population_not_preserved', self.report()['claims'][5]['reasons'])
        self.assertEqual(self.report()['claims'][5]['status'], 'uncertain')

    def test_real_citation_gate_still_rejects(self):
        from src.citation_validator import validate_citations
        r=validate_citations(self.ANSWER,self.sources())
        self.assertFalse(r['valid'])
        self.assertEqual(r['units_without_citations'],[1,3])

    def test_unrelated_source_cannot_support_detection(self):
        c='Implante edilebilir kalp cihazlarinin sorgulanmasi: Bu cihazlarin kayitlari incelenerek AF saptanabilir [1].'
        self.assertNotEqual(self.report(c,[source('CAD can be detected with wearable devices.')])['claims'][0]['status'],'supported')

    def test_added_clinical_benefit_is_not_ignored(self):
        c='EKG izlemi uzadikca AF saptanma olasiligi artar ve mortalite azalir [1].'
        self.assertNotEqual(self.report(c)['claims'][0]['status'],'supported')

    def test_negated_source_cannot_support_ecg_duration(self):
        c='EKG izlemi uzadikca AF saptanma olasiligi artar [1].'
        s=source('The probability of AF detection does not increase with electrocardiogram (ECG) monitoring duration.')
        self.assertNotEqual(self.report(c,[s])['claims'][0]['status'],'supported')

    def test_contradictory_screening_certainty_not_accepted(self):
        c='Genel populasyonda asemptomatik AF taramasinin yararlari kesindir [1].'
        self.assertNotEqual(self.report(c)['claims'][0]['status'],'supported')

    def test_only_own_citation_is_used(self):
        c='EKG izlemi uzadikca AF saptanma olasiligi artar [2].'
        self.assertNotEqual(self.report(c)['claims'][0]['status'],'supported')

    def test_unknown_number_cannot_support_fact(self):
        self.assertNotEqual(self.report('EKG izlemi uzadikca AF saptanma olasiligi artar [99].')['claims'][0]['status'],'supported')

    def test_unresolved_device_pronoun_is_not_supported(self):
        c='Bu cihazlarin kayitlari incelenerek AF saptanabilir [2].'
        self.assertNotEqual(self.report(c)['claims'][0]['status'],'supported')

    def test_tampered_source_hash_blocks_controlled_fact(self):
        s=self.sources();s[0]['source_text']+=' changed'
        self.assertNotEqual(self.report('EKG izlemi uzadikca AF saptanma olasiligi artar [1].',s)['claims'][0]['status'],'supported')


if __name__ == "__main__":
    unittest.main()
