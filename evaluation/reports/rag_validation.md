# RAG validation evaluation

Provisional diagnostics are NOT human-approved ground truth.
No LLM/API calls. No SQL writes. Answers are curated evaluation examples, not real model outputs.

Cases: 44; labels: {'supported': 17, 'unsupported': 16, 'partially_supported': 6, 'uncertain': 5}; review: {'provisional': 44}.

Positive class: accept a fully supported answer. Partial/uncertain cases require rejection by policy.
Uncertain does not mean factually false. Citation/guard failures are excluded from claim-only metrics.

| Metric | Human-confirmed claim labels | Provisional claim diagnostics |
|---|---:|---:|
| n | 0 | 39 |
| tp | not computable | 4 |
| fp | not computable | 0 |
| tn | not computable | 22 |
| fn | not computable | 13 |
| precision | not computable | 1.0000 |
| recall | not computable | 0.2353 |
| f1 | not computable | 0.3810 |
| false_accept_rate | not computable | 0.0000 |
| false_reject_rate | not computable | 0.7647 |

## Model execution

| Reranker | Embedding | Status | Questions measured |
|---|---|---|---:|
| minilm | minilm | measured | 14 |
| bge-m3 | minilm | not_available | 0 |

## Provisional full acceptance-policy diagnostics

| Component | n | TP | FP | TN | FN |
|---|---:|---:|---:|---:|---:|
| answer_acceptance_policy | 44 | 4 | 0 | 27 | 13 |
| evidence_guard_only | 3 | 0 | 0 | 3 | 0 |
| decisive_claim_labels_only | 36 | 4 | 0 | 19 | 13 |

## Observed retrieval diagnostics against known annotated chunks

| Model | Language | Stage | Annotated Recall@5 | Annotated MRR | Questions |
|---|---|---|---:|---:|---:|
| minilm | tr | retrieved | 0.5000 | 0.2725 | 7 |
| minilm | tr | reranked | 0.2143 | 0.1749 | 7 |
| minilm | tr | context | 0.2143 | 0.2143 | 7 |
| minilm | en | retrieved | 0.6429 | 0.7381 | 7 |
| minilm | en | reranked | 0.7143 | 0.6786 | 7 |
| minilm | en | context | 0.2143 | 0.2143 | 7 |

Recall@k/MRR require complete gold annotations for corpus-wide interpretation. nDCG@k also requires judged top-k results.
Annotated-gold diagnostics, when present in JSON, concern only explicitly listed positive chunks.

## Case decisions

| Case | Language | Component | Provisional label | Observed status | Policy agreement |
|---|---|---|---|---|---|
| AF01 | en | answer_validation | supported | accepted | True |
| AF02 | tr | answer_validation | supported | claim_validation_failed | False |
| AF03 | tr | answer_validation | unsupported | claim_validation_failed | True |
| AF04 | en | answer_validation | partially_supported | claim_validation_failed | True |
| AF05 | en | answer_validation | uncertain | claim_validation_failed | True |
| AF06 | en | answer_validation | unsupported | citation_validation_failed | True |
| AF07 | en | answer_validation | unsupported | claim_validation_failed | True |
| AF08 | en | answer_validation | supported | accepted | True |
| ROC01 | en | answer_validation | supported | claim_validation_failed | False |
| ROC02 | tr | answer_validation | supported | claim_validation_failed | False |
| ROC03 | en | answer_validation | supported | claim_validation_failed | False |
| ROC04 | en | answer_validation | unsupported | claim_validation_failed | True |
| ROC05 | tr | answer_validation | partially_supported | claim_validation_failed | True |
| ROC06 | en | answer_validation | partially_supported | claim_validation_failed | True |
| ROC07 | en | answer_validation | partially_supported | claim_validation_failed | True |
| ROC08 | tr | answer_validation | uncertain | claim_validation_failed | True |
| POP01 | en | answer_validation | supported | claim_validation_failed | False |
| POP02 | tr | answer_validation | supported | claim_validation_failed | False |
| POP03 | en | answer_validation | unsupported | claim_validation_failed | True |
| POP04 | en | answer_validation | unsupported | claim_validation_failed | True |
| HB01 | en | answer_validation | supported | claim_validation_failed | False |
| HB02 | tr | answer_validation | supported | claim_validation_failed | False |
| HB03 | en | answer_validation | unsupported | claim_validation_failed | True |
| HB04 | tr | answer_validation | uncertain | claim_validation_failed | True |
| HB05 | en | answer_validation | unsupported | claim_validation_failed | True |
| TRP01 | en | answer_validation | supported | claim_validation_failed | False |
| TRP02 | tr | answer_validation | supported | claim_validation_failed | False |
| TRP03 | en | answer_validation | unsupported | claim_validation_failed | True |
| TRP04 | en | answer_validation | unsupported | claim_validation_failed | True |
| TRP05 | tr | answer_validation | partially_supported | claim_validation_failed | True |
| TRP06 | en | answer_validation | unsupported | claim_validation_failed | True |
| TRP07 | tr | answer_validation | supported | claim_validation_failed | False |
| CAD01 | en | answer_validation | supported | accepted | True |
| CAD02 | tr | answer_validation | supported | claim_validation_failed | False |
| CAD03 | en | answer_validation | unsupported | claim_validation_failed | True |
| CAD04 | en | answer_validation | unsupported | claim_validation_failed | True |
| OHCA01 | en | answer_validation | supported | accepted | True |
| OHCA02 | tr | answer_validation | supported | claim_validation_failed | False |
| OHCA03 | en | answer_validation | unsupported | claim_validation_failed | True |
| OHCA04 | tr | answer_validation | unsupported | claim_validation_failed | True |
| GATE01 | tr | evidence_guard | uncertain | insufficient_evidence | True |
| GATE02 | en | evidence_guard | uncertain | insufficient_evidence | True |
| GATE03 | tr | evidence_guard | unsupported | insufficient_evidence | True |
| CITE01 | en | answer_validation | partially_supported | citation_validation_failed | True |

## Source-grounded review appendix

Every answer below is a constructed evaluation example, including deliberate errors. All labels remain provisional.

### AF01 — provisional

Question (en): How can atrial fibrillation be detected?

Evaluation answer: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices. [1]

Expected label: supported; expected decision: accept.

Rationale: Physical p2 explicitly lists these detection settings and methods.

Source [1]: pubM-Atrial Fibrillation.pdf, physical page 2.

Chunk: 9d2187931548b45d5f73a8b48a843e1bc64e576da52998bedcb0a70dcd254a39_vbc452dc54a8ed2fa_p2_c0_tba3617fdc550310f6b8ad42fd0e87b5a89ada9068cdd40849fa5a737597c0be0

Source quote: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices.

### AF02 — provisional

Question (tr): Atriyal fibrilasyon hangi yöntemlerle tespit edilebilir?

Evaluation answer: AF klinik karşılaşmalarda tesadüfen, giyilebilir cihazlarla veya implante kardiyak elektronik cihazların sorgulanmasıyla tespit edilebilir. [1]

Expected label: supported; expected decision: accept.

Rationale: Faithful Turkish paraphrase of p2; not a claim of diagnostic accuracy or treatment benefit.

Source [1]: pubM-Atrial Fibrillation.pdf, physical page 2.

Chunk: 9d2187931548b45d5f73a8b48a843e1bc64e576da52998bedcb0a70dcd254a39_vbc452dc54a8ed2fa_p2_c0_tba3617fdc550310f6b8ad42fd0e87b5a89ada9068cdd40849fa5a737597c0be0

Source quote: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices.

### AF03 — provisional

Question (tr): Atriyal fibrilasyon hangi yöntemlerle tespit edilebilir?

Evaluation answer: Giyilebilir cihazlarla AF tespitinin duyarlılığı %100 ve özgüllüğü %100 bulunmuştur. [1]

Expected label: unsupported; expected decision: reject.

Rationale: The cited passage provides methods, not sensitivity or specificity.

Source [1]: pubM-Atrial Fibrillation.pdf, physical page 2.

Chunk: 9d2187931548b45d5f73a8b48a843e1bc64e576da52998bedcb0a70dcd254a39_vbc452dc54a8ed2fa_p2_c0_tba3617fdc550310f6b8ad42fd0e87b5a89ada9068cdd40849fa5a737597c0be0

Source quote: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices.

### AF04 — provisional

Question (en): How can atrial fibrillation be detected?

Evaluation answer: Wearable devices can detect AF and prevent all ischemic strokes. [1]

Expected label: partially_supported; expected decision: reject.

Rationale: Detection is supported; stroke-prevention benefit is not established in this detection statement.

Source [1]: pubM-Atrial Fibrillation.pdf, physical page 2.

Chunk: 9d2187931548b45d5f73a8b48a843e1bc64e576da52998bedcb0a70dcd254a39_vbc452dc54a8ed2fa_p2_c0_tba3617fdc550310f6b8ad42fd0e87b5a89ada9068cdd40849fa5a737597c0be0

Source quote: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices.

### AF05 — provisional

Question (en): How can atrial fibrillation be detected?

Evaluation answer: Wearable devices are superior to all implanted devices for detecting AF. [1]

Expected label: uncertain; expected decision: reject.

Rationale: The source does not compare superiority; absence of comparison is not proof of the opposite.

Source [1]: pubM-Atrial Fibrillation.pdf, physical page 2.

Chunk: 9d2187931548b45d5f73a8b48a843e1bc64e576da52998bedcb0a70dcd254a39_vbc452dc54a8ed2fa_p2_c0_tba3617fdc550310f6b8ad42fd0e87b5a89ada9068cdd40849fa5a737597c0be0

Source quote: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices.

### AF06 — provisional

Question (en): How can atrial fibrillation be detected?

Evaluation answer: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices. [2]

Expected label: unsupported; expected decision: reject.

Rationale: Citation [2] does not exist in the supplied source list.

Source [1]: pubM-Atrial Fibrillation.pdf, physical page 2.

Chunk: 9d2187931548b45d5f73a8b48a843e1bc64e576da52998bedcb0a70dcd254a39_vbc452dc54a8ed2fa_p2_c0_tba3617fdc550310f6b8ad42fd0e87b5a89ada9068cdd40849fa5a737597c0be0

Source quote: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices.

### AF07 — provisional

Question (en): How can atrial fibrillation be detected?

Evaluation answer: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices. [2]

Expected label: unsupported; expected decision: reject.

Rationale: The asserted AF statement cites a CAD guideline chunk instead of AF p2.

Source [1]: pubM-Atrial Fibrillation.pdf, physical page 2.

Chunk: 9d2187931548b45d5f73a8b48a843e1bc64e576da52998bedcb0a70dcd254a39_vbc452dc54a8ed2fa_p2_c0_tba3617fdc550310f6b8ad42fd0e87b5a89ada9068cdd40849fa5a737597c0be0

Source quote: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices.

Source [2]: pubM-Clinical Updates in Coronary Artery Disease A comprehensive review.pdf, physical page 8.

Chunk: 1d5842c9f78b3edb071cd62429cafcfef2bd80e2048687e43b5a73c677515a21_vbc452dc54a8ed2fa_p8_c2_t116d0896c920435e04ee512149064faf3c43185e0b1021d41021481262253bd7

Source quote: FFR-CT can be useful for diagnosis of vessel-specific ischemia and to guide decision-making regarding the use of coronary revascularization (Table 3).

### AF08 — provisional

Question (en): How can atrial fibrillation be detected?

Evaluation answer: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices. [1,2]

Expected label: supported; expected decision: accept.

Rationale: Both citation numbers refer to the same real supporting passage; duplicate support is not two independent studies.

Source [1]: pubM-Atrial Fibrillation.pdf, physical page 2.

Chunk: 9d2187931548b45d5f73a8b48a843e1bc64e576da52998bedcb0a70dcd254a39_vbc452dc54a8ed2fa_p2_c0_tba3617fdc550310f6b8ad42fd0e87b5a89ada9068cdd40849fa5a737597c0be0

Source quote: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices.

Source [2]: pubM-Atrial Fibrillation.pdf, physical page 2.

Chunk: 9d2187931548b45d5f73a8b48a843e1bc64e576da52998bedcb0a70dcd254a39_vbc452dc54a8ed2fa_p2_c0_tba3617fdc550310f6b8ad42fd0e87b5a89ada9068cdd40849fa5a737597c0be0

Source quote: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices.

### ROC01 — provisional

Question (en): What ROC discrimination was reported for ELR and PLR in critical carotid stenosis?

Evaluation answer: ELR AUC=0.633, 95% CI: 0.562-0.705, p<0.001. [1]

Expected label: supported; expected decision: accept.

Rationale: Figure1A on physical p5 reports this exact ELR AUC, CI and ROC p.

Source [1]: TK-cardiovascular37-2-1.pdf, physical page 5.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p5_c0_t8e9ac4faf16b5d2b88afa1b96102a4e4653a7ba68ab7cd730d469bfa8640a1e9

Source quote: ROC curve for ELR showing an A u C of 0.633 (95% CI: 0.562-0.705, p<0.001), indicating a moderate discriminative ability;

### ROC02 — provisional

Question (tr): Kritik karotis darlığında ELR ve PLR için ROC sonuçları nelerdir?

Evaluation answer: ELR için AUC=0,633, %95 GA: 0,562–0,705 ve p<0,001 olarak bildirilmiştir. [1]

Expected label: supported; expected decision: accept.

Rationale: Turkish numeric paraphrase of Figure1A; labels are source-reviewed candidates, not validator-generated.

Source [1]: TK-cardiovascular37-2-1.pdf, physical page 5.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p5_c0_t8e9ac4faf16b5d2b88afa1b96102a4e4653a7ba68ab7cd730d469bfa8640a1e9

Source quote: ROC curve for ELR showing an A u C of 0.633 (95% CI: 0.562-0.705, p<0.001), indicating a moderate discriminative ability;

### ROC03 — provisional

Question (en): What ROC discrimination was reported for ELR and PLR in critical carotid stenosis?

Evaluation answer: PLR AUC=0.627, 95% CI: 0.556-0.698, p=0.002. [1]

Expected label: supported; expected decision: accept.

Rationale: Figure1B reports PLR values separately from ELR.

Source [1]: TK-cardiovascular37-2-1.pdf, physical page 5.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p5_c0_t8e9ac4faf16b5d2b88afa1b96102a4e4653a7ba68ab7cd730d469bfa8640a1e9

Source quote: ROC curve for ELR showing an A u C of 0.633 (95% CI: 0.562-0.705, p<0.001), indicating a moderate discriminative ability;

### ROC04 — provisional

Question (en): What ROC discrimination was reported for ELR and PLR in critical carotid stenosis?

Evaluation answer: ELR AUC=0.627, 95% CI: 0.556-0.698, p=0.002. [1]

Expected label: unsupported; expected decision: reject.

Rationale: These numbers belong to PLR, not ELR, in the same chunk.

Source [1]: TK-cardiovascular37-2-1.pdf, physical page 5.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p5_c0_t8e9ac4faf16b5d2b88afa1b96102a4e4653a7ba68ab7cd730d469bfa8640a1e9

Source quote: ROC curve for ELR showing an A u C of 0.633 (95% CI: 0.562-0.705, p<0.001), indicating a moderate discriminative ability;

### ROC05 — provisional

Question (tr): Kritik karotis darlığında ELR ve PLR için ROC sonuçları nelerdir?

Evaluation answer: ELR için AUC=0,633 ve %95 GA: 0,556–0,698 bulunmuştur. [1]

Expected label: partially_supported; expected decision: reject.

Rationale: The ELR AUC is correct but the CI is PLR-specific.

Source [1]: TK-cardiovascular37-2-1.pdf, physical page 5.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p5_c0_t8e9ac4faf16b5d2b88afa1b96102a4e4653a7ba68ab7cd730d469bfa8640a1e9

Source quote: ROC curve for ELR showing an A u C of 0.633 (95% CI: 0.562-0.705, p<0.001), indicating a moderate discriminative ability;

### ROC06 — provisional

Question (en): What ROC discrimination was reported for ELR and PLR in critical carotid stenosis?

Evaluation answer: ELR AUC=0.633, p=0.010. [1]

Expected label: partially_supported; expected decision: reject.

Rationale: AUC is correct; p0.010 is a group-comparison p from p3, not the ROC p on cited p5.

Source [1]: TK-cardiovascular37-2-1.pdf, physical page 5.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p5_c0_t8e9ac4faf16b5d2b88afa1b96102a4e4653a7ba68ab7cd730d469bfa8640a1e9

Source quote: ROC curve for ELR showing an A u C of 0.633 (95% CI: 0.562-0.705, p<0.001), indicating a moderate discriminative ability;

### ROC07 — provisional

Question (en): What ROC discrimination was reported for ELR and PLR in critical carotid stenosis?

Evaluation answer: ELR AUC=0.933, p<0.001. [1]

Expected label: partially_supported; expected decision: reject.

Rationale: ROC p is correct; the AUC was changed from0.633 to0.933.

Source [1]: TK-cardiovascular37-2-1.pdf, physical page 5.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p5_c0_t8e9ac4faf16b5d2b88afa1b96102a4e4653a7ba68ab7cd730d469bfa8640a1e9

Source quote: ROC curve for ELR showing an A u C of 0.633 (95% CI: 0.562-0.705, p<0.001), indicating a moderate discriminative ability;

### ROC08 — provisional

Question (tr): Kritik karotis darlığında ELR ve PLR için ROC sonuçları nelerdir?

Evaluation answer: ELR kritik karotis darlığının tek başına kesin tanısını koydurur. [1]

Expected label: uncertain; expected decision: reject.

Rationale: An AUC estimate does not establish standalone definitive diagnostic utility.

Source [1]: TK-cardiovascular37-2-1.pdf, physical page 5.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p5_c0_t8e9ac4faf16b5d2b88afa1b96102a4e4653a7ba68ab7cd730d469bfa8640a1e9

Source quote: ROC curve for ELR showing an A u C of 0.633 (95% CI: 0.562-0.705, p<0.001), indicating a moderate discriminative ability;

### POP01 — provisional

Question (en): How many patients and controls were included in the carotid stenosis study?

Evaluation answer: The study group (Group 1) consisted of 96 patients who underwent carotid endarterectomy due to ≥ 70% CAS. [1]

Expected label: supported; expected decision: accept.

Rationale: Methods p2 identifies the96 patients with>=70% CAS who underwent endarterectomy.

Source [1]: TK-cardiovascular37-2-1.pdf, physical page 2.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p2_c2_t2ccc3b5b7d835ea6270e21168199519ec8d152e409d7cc3047313156133a4df4

Source quote: The study group (Group 1) consisted of 96 patients who underwent carotid endarterectomy due to ≥ 70% CAS.

### POP02 — provisional

Question (tr): Karotis darlığı çalışmasının grupları kaç kişidir?

Evaluation answer: Çalışma grubu 96 hasta ve kontrol grubu 93 kişiden oluşmuştur. [1,2]

Expected label: supported; expected decision: accept.

Rationale: Methods p2 and Results p3 agree:96 cases,93 controls,total189.

Source [1]: TK-cardiovascular37-2-1.pdf, physical page 2.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p2_c2_t2ccc3b5b7d835ea6270e21168199519ec8d152e409d7cc3047313156133a4df4

Source quote: The study group (Group 1) consisted of 96 patients who underwent carotid endarterectomy due to ≥ 70% CAS.

Source [2]: TK-cardiovascular37-2-1.pdf, physical page 3.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p3_c1_t96f030eb24e3d1e32308b240cfba5ab9fc78b19bd52a77b0f38c007e2a0445cd

Source quote: In this study, 189 patients were examined. 96 patients were in the study group and 93 patients were in the control group.

### POP03 — provisional

Question (en): How many patients and controls were included in the carotid stenosis study?

Evaluation answer: Study group n=93 and control group n=96. [1,2]

Expected label: unsupported; expected decision: reject.

Rationale: Case and control counts are reversed.

Source [1]: TK-cardiovascular37-2-1.pdf, physical page 2.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p2_c2_t2ccc3b5b7d835ea6270e21168199519ec8d152e409d7cc3047313156133a4df4

Source quote: The study group (Group 1) consisted of 96 patients who underwent carotid endarterectomy due to ≥ 70% CAS.

Source [2]: TK-cardiovascular37-2-1.pdf, physical page 3.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p3_c1_t96f030eb24e3d1e32308b240cfba5ab9fc78b19bd52a77b0f38c007e2a0445cd

Source quote: In this study, 189 patients were examined. 96 patients were in the study group and 93 patients were in the control group.

### POP04 — provisional

Question (en): How many patients and controls were included in the carotid stenosis study?

Evaluation answer: All 189 participants underwent carotid endarterectomy. [1,2]

Expected label: unsupported; expected decision: reject.

Rationale: Only the96 case patients underwent endarterectomy;93 controls had no plaques/thickening.

Source [1]: TK-cardiovascular37-2-1.pdf, physical page 2.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p2_c2_t2ccc3b5b7d835ea6270e21168199519ec8d152e409d7cc3047313156133a4df4

Source quote: The study group (Group 1) consisted of 96 patients who underwent carotid endarterectomy due to ≥ 70% CAS.

Source [2]: TK-cardiovascular37-2-1.pdf, physical page 3.

Chunk: 261d84fa68ee3188bd807ce41aff494ac1885abe78074b98b068c06e20371cd0_vbc452dc54a8ed2fa_p3_c1_t96f030eb24e3d1e32308b240cfba5ab9fc78b19bd52a77b0f38c007e2a0445cd

Source quote: In this study, 189 patients were examined. 96 patients were in the study group and 93 patients were in the control group.

### HB01 — provisional

Question (en): What was the HbA1c ROC result for ischemic events in AF patients?

Evaluation answer: HbA1c AUC=0.712 with 95% CI: 0.542-0.763. [1]

Expected label: supported; expected decision: accept.

Rationale: Own ROC Results p5 report HbA1c, distinct from HCR AUC0.821.

Source [1]: TK-cardiovascular36-2-1.pdf, physical page 5.

Chunk: 795f2506c5bd4f750de3899a8c22d3af6fcdb79415599ea35bfe2a75e3fb22b7_vbc452dc54a8ed2fa_p5_c0_t3a040a426d7069410376042d61ce91f30c15149e2f5c5246d378b4c7a25bb9ec

Source quote: In the ROC analysis, the cut-off value of the HbA1c value in patients experiencing AF and ischemic events was found to be 6.82, with 71% sensitivity and 70% specificity (AUC=0.712, CI95=0.542-0.763, p<001) (Figure 1).

### HB02 — provisional

Question (tr): AF hastalarında HbA1c iskemik olayları nasıl ayırt etmiştir?

Evaluation answer: HbA1c için AUC 0,712 ve %95 GA 0,542–0,763 bildirilmiştir. [1]

Expected label: supported; expected decision: accept.

Rationale: Faithful Turkish statement of the own HbA1c result, not the external discussion quote.

Source [1]: TK-cardiovascular36-2-1.pdf, physical page 5.

Chunk: 795f2506c5bd4f750de3899a8c22d3af6fcdb79415599ea35bfe2a75e3fb22b7_vbc452dc54a8ed2fa_p5_c0_t3a040a426d7069410376042d61ce91f30c15149e2f5c5246d378b4c7a25bb9ec

Source quote: In the ROC analysis, the cut-off value of the HbA1c value in patients experiencing AF and ischemic events was found to be 6.82, with 71% sensitivity and 70% specificity (AUC=0.712, CI95=0.542-0.763, p<001) (Figure 1).

### HB03 — provisional

Question (en): What was the HbA1c ROC result for ischemic events in AF patients?

Evaluation answer: HbA1c AUC=0.821, 95% CI: 0.682-0.889. [1]

Expected label: unsupported; expected decision: reject.

Rationale: The changed values are HCR HbA1c/C-peptide values, not HbA1c.

Source [1]: TK-cardiovascular36-2-1.pdf, physical page 5.

Chunk: 795f2506c5bd4f750de3899a8c22d3af6fcdb79415599ea35bfe2a75e3fb22b7_vbc452dc54a8ed2fa_p5_c0_t3a040a426d7069410376042d61ce91f30c15149e2f5c5246d378b4c7a25bb9ec

Source quote: In the ROC analysis, the cut-off value of the HbA1c value in patients experiencing AF and ischemic events was found to be 6.82, with 71% sensitivity and 70% specificity (AUC=0.712, CI95=0.542-0.763, p<001) (Figure 1).

### HB04 — provisional

Question (tr): HbA1c ROC p değeri nedir?

Evaluation answer: HbA1c ROC p<0,001 olarak kesin doğrulanmıştır. [1]

Expected label: uncertain; expected decision: reject.

Rationale: The publication literally prints p<001; no editorial correction or exact p may be invented.

Source [1]: TK-cardiovascular36-2-1.pdf, physical page 5.

Chunk: 795f2506c5bd4f750de3899a8c22d3af6fcdb79415599ea35bfe2a75e3fb22b7_vbc452dc54a8ed2fa_p5_c0_t3a040a426d7069410376042d61ce91f30c15149e2f5c5246d378b4c7a25bb9ec

Source quote: In the ROC analysis, the cut-off value of the HbA1c value in patients experiencing AF and ischemic events was found to be 6.82, with 71% sensitivity and 70% specificity (AUC=0.712, CI95=0.542-0.763, p<001) (Figure 1).

### HB05 — provisional

Question (en): Was the cohort restricted to diabetic AF patients?

Evaluation answer: All 1225 analyzed AF patients had diabetes. [1,2]

Expected label: unsupported; expected decision: reject.

Rationale: The cohort includes AF patients, not an exclusively diabetic population; final1225 excludes300 missing-data records.

Source [1]: TK-cardiovascular36-2-1.pdf, physical page 5.

Chunk: 795f2506c5bd4f750de3899a8c22d3af6fcdb79415599ea35bfe2a75e3fb22b7_vbc452dc54a8ed2fa_p5_c0_t3a040a426d7069410376042d61ce91f30c15149e2f5c5246d378b4c7a25bb9ec

Source quote: In the ROC analysis, the cut-off value of the HbA1c value in patients experiencing AF and ischemic events was found to be 6.82, with 71% sensitivity and 70% specificity (AUC=0.712, CI95=0.542-0.763, p<001) (Figure 1).

Source [2]: TK-cardiovascular36-2-1.pdf, physical page 2.

Chunk: 795f2506c5bd4f750de3899a8c22d3af6fcdb79415599ea35bfe2a75e3fb22b7_vbc452dc54a8ed2fa_p2_c2_t81c2c997226ade508854ece8f9eb179155d3120179f356948ff336bd71644d6c

Source quote: The study was completed with 1,225 patients.

### TRP01 — provisional

Question (en): What was the 1-3 hour delta hs-troponin I ROC result for AMI?

Evaluation answer: According to the cut-off value, the maximum diagnostic accuracy is delta HS between 1-3 hours troponin I >47.2% had a sensitivity (80.0%) and specificity (89.4%) with AUC of 0.838 ± 0.066 (p <0.001). [1]

Expected label: supported; expected decision: accept.

Rationale: Own p4 statement corresponds to TableII p3; ±0.066 is SE per table, not CI.

Source [1]: who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf, physical page 4.

Chunk: 23d942b88a96b0c4539deb4bbaa064a17d8b97cbabf408c97b2cce6c9c9eb67c_vbc452dc54a8ed2fa_p4_c2_t827b6d2c035bf3d55fa984a1571e7c4d1802244d085b9c24cfaf352c018fb4f1

Source quote: According to the cut-off value, the maximum diagnostic accuracy is delta HS between 1-3 hours troponin I >47.2% had a sensitivity (80.0%) and specificity (89.4%) with AUC of 0.838 ± 0.066 (p <0.001).

### TRP02 — provisional

Question (tr): 1–3 saatlik delta hs-troponin I için AMI ROC sonucu nedir?

Evaluation answer: 1–3 saatlik delta hs-troponin I için AUC=0,838 ve p<0,001 bildirilmiştir. [1,2]

Expected label: supported; expected decision: accept.

Rationale: P3 TableII and p4 own results identify the1-3h measurement and AUC0.838.

Source [1]: who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf, physical page 4.

Chunk: 23d942b88a96b0c4539deb4bbaa064a17d8b97cbabf408c97b2cce6c9c9eb67c_vbc452dc54a8ed2fa_p4_c2_t827b6d2c035bf3d55fa984a1571e7c4d1802244d085b9c24cfaf352c018fb4f1

Source quote: According to the cut-off value, the maximum diagnostic accuracy is delta HS between 1-3 hours troponin I >47.2% had a sensitivity (80.0%) and specificity (89.4%) with AUC of 0.838 ± 0.066 (p <0.001).

Source [2]: who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf, physical page 3.

Chunk: 23d942b88a96b0c4539deb4bbaa064a17d8b97cbabf408c97b2cce6c9c9eb67c_vbc452dc54a8ed2fa_p3_c2_t9d05e34c56e8823513fb034190895d3b1e0918daa6e657e78f793d7dee0fd148

Source quote: |AUC±SE.|0.755 ± 0.072|0.809 ± 0.074|0.838 ± 0.066|

### TRP03 — provisional

Question (en): What was the 1-3 hour delta hs-troponin I ROC result for AMI?

Evaluation answer: Delta hs troponin I (0-1h) AUC=0.838. [1]

Expected label: unsupported; expected decision: reject.

Rationale: AUC0.838 belongs to1-3h;0-1h AUC is0.755.

Source [1]: who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf, physical page 4.

Chunk: 23d942b88a96b0c4539deb4bbaa064a17d8b97cbabf408c97b2cce6c9c9eb67c_vbc452dc54a8ed2fa_p4_c2_t827b6d2c035bf3d55fa984a1571e7c4d1802244d085b9c24cfaf352c018fb4f1

Source quote: According to the cut-off value, the maximum diagnostic accuracy is delta HS between 1-3 hours troponin I >47.2% had a sensitivity (80.0%) and specificity (89.4%) with AUC of 0.838 ± 0.066 (p <0.001).

### TRP04 — provisional

Question (en): What was the 1-3 hour delta hs-troponin I ROC result for AMI?

Evaluation answer: Delta hs troponin I (1-3h) AUC=0.838, 95% CI: 0.772-0.904. [1,2]

Expected label: unsupported; expected decision: reject.

Rationale: The CI was inferred from a standard error and is not published; do not manufacture it.

Source [1]: who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf, physical page 4.

Chunk: 23d942b88a96b0c4539deb4bbaa064a17d8b97cbabf408c97b2cce6c9c9eb67c_vbc452dc54a8ed2fa_p4_c2_t827b6d2c035bf3d55fa984a1571e7c4d1802244d085b9c24cfaf352c018fb4f1

Source quote: According to the cut-off value, the maximum diagnostic accuracy is delta HS between 1-3 hours troponin I >47.2% had a sensitivity (80.0%) and specificity (89.4%) with AUC of 0.838 ± 0.066 (p <0.001).

Source [2]: who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf, physical page 3.

Chunk: 23d942b88a96b0c4539deb4bbaa064a17d8b97cbabf408c97b2cce6c9c9eb67c_vbc452dc54a8ed2fa_p3_c2_t9d05e34c56e8823513fb034190895d3b1e0918daa6e657e78f793d7dee0fd148

Source quote: |AUC±SE.|0.755 ± 0.072|0.809 ± 0.074|0.838 ± 0.066|

### TRP05 — provisional

Question (tr): 1–3 saatlik delta hs-troponin I için AMI ROC sonucu nedir?

Evaluation answer: 1–3 saatlik delta hs-troponin I eşiği >%66,7, duyarlılığı %80 ve özgüllüğü %89,4 bulunmuştur. [1,2]

Expected label: partially_supported; expected decision: reject.

Rationale: Sensitivity/specificity are1-3h values but cutoff66.7 belongs to0-1h; correct1-3h cutoff47.2.

Source [1]: who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf, physical page 4.

Chunk: 23d942b88a96b0c4539deb4bbaa064a17d8b97cbabf408c97b2cce6c9c9eb67c_vbc452dc54a8ed2fa_p4_c2_t827b6d2c035bf3d55fa984a1571e7c4d1802244d085b9c24cfaf352c018fb4f1

Source quote: According to the cut-off value, the maximum diagnostic accuracy is delta HS between 1-3 hours troponin I >47.2% had a sensitivity (80.0%) and specificity (89.4%) with AUC of 0.838 ± 0.066 (p <0.001).

Source [2]: who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf, physical page 3.

Chunk: 23d942b88a96b0c4539deb4bbaa064a17d8b97cbabf408c97b2cce6c9c9eb67c_vbc452dc54a8ed2fa_p3_c2_t9d05e34c56e8823513fb034190895d3b1e0918daa6e657e78f793d7dee0fd148

Source quote: |AUC±SE.|0.755 ± 0.072|0.809 ± 0.074|0.838 ± 0.066|

### TRP06 — provisional

Question (en): Did this study report sensitivity32.6% at the250% cutoff?

Evaluation answer: This study found sensitivity32.6%, specificity99.6% and PPV95.8 at a250% cutoff. [1]

Expected label: unsupported; expected decision: reject.

Rationale: The cited passage explicitly attributes these values to Keller et al., not the current study.

Source [1]: who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf, physical page 4.

Chunk: 23d942b88a96b0c4539deb4bbaa064a17d8b97cbabf408c97b2cce6c9c9eb67c_vbc452dc54a8ed2fa_p4_c3_t9990850da5eeebe87694f395344a38e0a7bd6b0f76de9f47fbc149941a38eac7

Source quote: In the study conducted by Keller

### TRP07 — provisional

Question (tr): Troponin çalışmasında kaç hasta değerlendirilmiştir?

Evaluation answer: Troponin çalışmasında 114 hasta değerlendirilmiştir. [1]

Expected label: supported; expected decision: accept.

Rationale: Results p2 states final114;20 AMI and94 without AMI are described separately.

Source [1]: who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng.pdf, physical page 2.

Chunk: 23d942b88a96b0c4539deb4bbaa064a17d8b97cbabf408c97b2cce6c9c9eb67c_vbc452dc54a8ed2fa_p2_c3_te06b73cf9f67108e8a5e040d9d88e28a99abb7584054ad354e9887e926a6351d

Source quote: The final evaluation was made on 114 patients.

### CAD01 — provisional

Question (en): How is FFR-CT positioned in the chest pain guideline cited by the review?

Evaluation answer: FFR-CT can be useful for diagnosis of vessel-specific ischemia and to guide decision-making regarding the use of coronary revascularization (Table 3). [1]

Expected label: supported; expected decision: accept.

Rationale: P8 guideline discussion supports vessel-specific ischemia diagnosis and decision-making; this is a recommendation, not an RCT effect.

Source [1]: pubM-Clinical Updates in Coronary Artery Disease A comprehensive review.pdf, physical page 8.

Chunk: 1d5842c9f78b3edb071cd62429cafcfef2bd80e2048687e43b5a73c677515a21_vbc452dc54a8ed2fa_p8_c2_t116d0896c920435e04ee512149064faf3c43185e0b1021d41021481262253bd7

Source quote: FFR-CT can be useful for diagnosis of vessel-specific ischemia and to guide decision-making regarding the use of coronary revascularization (Table 3).

### CAD02 — provisional

Question (tr): Derlemede FFR-CT hangi öneri sınıfıyla aktarılmıştır?

Evaluation answer: Orta-yüksek riskli akut veya stabil göğüs ağrılı ve CCTA proksimal/orta segmentinde %40–90 stenoz bulunan hastalarda FFR-CT Sınıf IIa önerisi olarak aktarılmıştır. [1]

Expected label: supported; expected decision: accept.

Rationale: The review explicitly quotes the classIIa recommendation for this population.

Source [1]: pubM-Clinical Updates in Coronary Artery Disease A comprehensive review.pdf, physical page 8.

Chunk: 1d5842c9f78b3edb071cd62429cafcfef2bd80e2048687e43b5a73c677515a21_vbc452dc54a8ed2fa_p8_c2_t116d0896c920435e04ee512149064faf3c43185e0b1021d41021481262253bd7

Source quote: FFR-CT can be useful for diagnosis of vessel-specific ischemia and to guide decision-making regarding the use of coronary revascularization (Table 3).

### CAD03 — provisional

Question (en): How is FFR-CT positioned in the chest pain guideline cited by the review?

Evaluation answer: This randomized FFR-CT trial enrolled400 patients and demonstrated a9.5% mortality reduction. [1]

Expected label: unsupported; expected decision: reject.

Rationale: The review contains a separate400-patient CCTA-guided PCI trial;9.5% is procedural success difference, not FFR-CT mortality benefit.

Source [1]: pubM-Clinical Updates in Coronary Artery Disease A comprehensive review.pdf, physical page 8.

Chunk: 1d5842c9f78b3edb071cd62429cafcfef2bd80e2048687e43b5a73c677515a21_vbc452dc54a8ed2fa_p8_c2_t116d0896c920435e04ee512149064faf3c43185e0b1021d41021481262253bd7

Source quote: FFR-CT can be useful for diagnosis of vessel-specific ischemia and to guide decision-making regarding the use of coronary revascularization (Table 3).

### CAD04 — provisional

Question (en): How is FFR-CT positioned in the chest pain guideline cited by the review?

Evaluation answer: The guideline gives FFR-CT a classI recommendation in all patients. [1]

Expected label: unsupported; expected decision: reject.

Rationale: ClassIIa is changed toI and the population is broadened to all patients.

Source [1]: pubM-Clinical Updates in Coronary Artery Disease A comprehensive review.pdf, physical page 8.

Chunk: 1d5842c9f78b3edb071cd62429cafcfef2bd80e2048687e43b5a73c677515a21_vbc452dc54a8ed2fa_p8_c2_t116d0896c920435e04ee512149064faf3c43185e0b1021d41021481262253bd7

Source quote: FFR-CT can be useful for diagnosis of vessel-specific ischemia and to guide decision-making regarding the use of coronary revascularization (Table 3).

### OHCA01 — provisional

Question (en): What evidence was identified for Bahrain in the GCC OHCA scoping review?

Evaluation answer: No literature was identified for Bahrain. [1]

Expected label: supported; expected decision: accept.

Rationale: Abstract Results p1 explicitly reports no literature identified for Bahrain; this is a review search result, not proof no events occurred.

Source [1]: who-Out-of-hospital cardiac arrest in countries of the Gulf Cooperation.pdf, physical page 1.

Chunk: 331dba2b95bc1d85a7e95d3b9b3a27ff0dbb13001a5f27e1306bc9b2833e453f_vbc452dc54a8ed2fa_p1_c1_t6c73a255d0541f6e3a84324e16ff0a3b9defd0a1de1d5b728c574f185130061c

Source quote: Of 647 citations retrieved, 24 studies were included for data extraction and analysis.

### OHCA02 — provisional

Question (tr): OHCA derlemesinde Bahreyn için hangi literatür bulunmuştur?

Evaluation answer: Derleme taramasında Bahreyn için literatür saptanmamıştır. [1]

Expected label: supported; expected decision: accept.

Rationale: Faithful translation of a bounded search finding, not absolute absence of OHCA.

Source [1]: who-Out-of-hospital cardiac arrest in countries of the Gulf Cooperation.pdf, physical page 1.

Chunk: 331dba2b95bc1d85a7e95d3b9b3a27ff0dbb13001a5f27e1306bc9b2833e453f_vbc452dc54a8ed2fa_p1_c1_t6c73a255d0541f6e3a84324e16ff0a3b9defd0a1de1d5b728c574f185130061c

Source quote: Of 647 citations retrieved, 24 studies were included for data extraction and analysis.

### OHCA03 — provisional

Question (en): What evidence was identified for Bahrain in the GCC OHCA scoping review?

Evaluation answer: The review included24 cardiac arrest patients. [1]

Expected label: unsupported; expected decision: reject.

Rationale: The number24 refers to studies, not patients.

Source [1]: who-Out-of-hospital cardiac arrest in countries of the Gulf Cooperation.pdf, physical page 1.

Chunk: 331dba2b95bc1d85a7e95d3b9b3a27ff0dbb13001a5f27e1306bc9b2833e453f_vbc452dc54a8ed2fa_p1_c1_t6c73a255d0541f6e3a84324e16ff0a3b9defd0a1de1d5b728c574f185130061c

Source quote: Of 647 citations retrieved, 24 studies were included for data extraction and analysis.

### OHCA04 — provisional

Question (tr): OHCA derlemesinde Bahreyn için hangi sonuç bildirilmiştir?

Evaluation answer: Bahreyn’de hastane dışı kardiyak arrest hiç gerçekleşmemiştir. [1]

Expected label: unsupported; expected decision: reject.

Rationale: No identified literature does not mean no cardiac arrests occurred.

Source [1]: who-Out-of-hospital cardiac arrest in countries of the Gulf Cooperation.pdf, physical page 1.

Chunk: 331dba2b95bc1d85a7e95d3b9b3a27ff0dbb13001a5f27e1306bc9b2833e453f_vbc452dc54a8ed2fa_p1_c1_t6c73a255d0541f6e3a84324e16ff0a3b9defd0a1de1d5b728c574f185130061c

Source quote: Of 647 citations retrieved, 24 studies were included for data extraction and analysis.

### GATE01 — provisional

Question (tr): Atriyal fibrilasyon hangi yöntemlerle tespit edilebilir?

Evaluation answer: AF giyilebilir cihazlarla tespit edilebilir. [1]

Expected label: uncertain; expected decision: reject.

Rationale: No sources were supplied; reject before generation regardless of corpus answerability.

Provided sources: none (controlled missing-evidence scenario).

### GATE02 — provisional

Question (en): How can atrial fibrillation be detected?

Evaluation answer: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices. [1]

Expected label: uncertain; expected decision: reject.

Rationale: Only the literal excerpt AF is provided as a controlled truncation fault; it is too short to count as evidence.

Source [1]: pubM-Atrial Fibrillation.pdf, physical page 2.

Chunk: 9d2187931548b45d5f73a8b48a843e1bc64e576da52998bedcb0a70dcd254a39_vbc452dc54a8ed2fa_p2_c0_tba3617fdc550310f6b8ad42fd0e87b5a89ada9068cdd40849fa5a737597c0be0

Source quote: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices.

### GATE03 — provisional

Question (tr): Atriyal fibrilasyon hangi yöntemlerle tespit edilebilir?

Evaluation answer: AF giyilebilir cihazlarla tespit edilebilir. [1]

Expected label: unsupported; expected decision: reject.

Rationale: CAD p8 is not AF detection evidence; an irrelevant but long paragraph must not pass.

Source [1]: pubM-Clinical Updates in Coronary Artery Disease A comprehensive review.pdf, physical page 8.

Chunk: 1d5842c9f78b3edb071cd62429cafcfef2bd80e2048687e43b5a73c677515a21_vbc452dc54a8ed2fa_p8_c2_t116d0896c920435e04ee512149064faf3c43185e0b1021d41021481262253bd7

Source quote: FFR-CT can be useful for diagnosis of vessel-specific ischemia and to guide decision-making regarding the use of coronary revascularization (Table 3).

### CITE01 — provisional

Question (en): How can atrial fibrillation be detected?

Evaluation answer: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices. This method has100% sensitivity. [1]

Expected label: partially_supported; expected decision: reject.

Rationale: The first statement is supported, the additional clinical accuracy statement has no source.

Source [1]: pubM-Atrial Fibrillation.pdf, physical page 2.

Chunk: 9d2187931548b45d5f73a8b48a843e1bc64e576da52998bedcb0a70dcd254a39_vbc452dc54a8ed2fa_p2_c0_tba3617fdc550310f6b8ad42fd0e87b5a89ada9068cdd40849fa5a737597c0be0

Source quote: AF can be detected incidentally during clinical encounters, with wearable devices, or through interrogation of cardiac implanted electronic devices.

## Limitations

- all provisional labels require independent expert review
- no end-to-end generated-answer performance measured
- no threshold fitted or applied
- gold retrieval pools are incomplete; unjudged chunks are not negatives
- shared passages/language variants are dependent; no held-out generalization claim
