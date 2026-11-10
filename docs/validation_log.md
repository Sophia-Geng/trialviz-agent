# Manual validation against clinicaltrials.gov (website)

Independent checks done by hand on the public website, compared with this system's output.
Counts on the website change daily; small differences are expected.

| # | Check | Website | This system | Date | Result |
|---|---|---|---|---|---|
| 4 | Intervention search "pembrolizumab", all years | 3005 (a first attempt showed 3055 but could not be reproduced, likely a different search box) | API default `query.intr`: 3005; strict search used by this system: 2659 | 2026-11-10 | Website = API default search exactly. The website's default search is ~13% broader than the strict search; spot checks of the extra trials (e.g. NCT03719105) found no mention of the drug in any field, which is why the system uses the strict AREA[InterventionName] / AREA[InterventionMeshTerm] search |
| 5a | NCT02704156 interventions | "Device: Cyberknife plus Pembrolizumab and Trametinib"; "Device: Cyberknife plus Gemcitabine" | same raw names and DEVICE type from the API; `drug` dimension resolves pembrolizumab (and trametinib / gemcitabine when confirmed elsewhere in the batch) | 2026-11-10 | Match. Confirms drug regimens registered as devices exist, motivating batch resolution of tentative drug names |
| 5a | NCT02704156 title / sponsor | "SBRT Plus Pembrolizumab and Trametinib for Pancreatic Cancer" / Changhai Hospital | identical `briefTitle` / `leadSponsor.name` | 2026-11-10 | Match |

| 6 | Brand vs generic name, strict search (API, full NCT ID sets) | – | Keytruda 2659 = pembrolizumab 2659 (0 differing IDs); Opdivo 1766 = nivolumab 1766 (0 differing) | 2026-11-10 | Brand names need no synonym table. A live question "Keytruda vs Opdivo — how do their trials break down by phase?" produced the same bars as example 03 |
| 7 | Plausibility: unscoped drug ↔ drug network (example 11) | – | Clusters = known regimens: cyclophosphamide–fludarabine 27 (CAR-T lymphodepletion), FOLFOX/FOLFIRI, R-CHOP, carboplatin–paclitaxel 24, azacitidine–venetoclax 14 | 2026-11-10 | Plausible. An earlier run of the same question exposed non-drug nodes (Biopsy, MRI, Control Group, "1", "the"), which were then filtered |
| 1 | Condition "Alzheimer's disease", total | matches | 4299 | 2026-11-10 | Match |
| 2 | Same search, Phase filter = Phase 3 | matches | Phase 3 (317) + Phase 2/3 (68) = 385 | 2026-11-10 | Match. The website's Phase 3 filter includes combined Phase 2/3 trials; this system gives combined phases their own bar so phase bars sum to the total |
| 3 | Condition "breast cancer", status Recruiting | matches | 2451 | 2026-11-10 | Match |
| 5b | NCT00065767 phase | Phase 2/3 | "Phase 2/3" (raw `["PHASE2","PHASE3"]`) | 2026-11-10 | Match |
