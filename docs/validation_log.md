# Manual validation against clinicaltrials.gov (website)

Independent checks done by hand on the public website, compared with this system's output.
Counts on the website change daily; small differences are expected.

| # | Check | Website | This system | Date | Result |
|---|---|---|---|---|---|
| 4 | Intervention search "pembrolizumab", all years | 3005 (a first attempt showed 3055 but could not be reproduced, likely a different search box) | API default `query.intr`: 3005; strict search used by this system: 2659 | 2026-11-10 | Website = API default search exactly. The website's default search is ~13% broader than the strict search; spot checks of the extra trials (e.g. NCT03719105) found no mention of the drug in any field, which is why the system uses the strict AREA[InterventionName] / AREA[InterventionMeshTerm] search |
| 5a | NCT02704156 interventions | "Device: Cyberknife plus Pembrolizumab and Trametinib"; "Device: Cyberknife plus Gemcitabine" | same raw names and DEVICE type from the API; `drug` dimension resolves pembrolizumab (and trametinib / gemcitabine when confirmed elsewhere in the batch) | 2026-11-10 | Match. Confirms drug regimens registered as devices exist, motivating batch resolution of tentative drug names |
| 5a | NCT02704156 title / sponsor | "SBRT Plus Pembrolizumab and Trametinib for Pancreatic Cancer" / Changhai Hospital | identical `briefTitle` / `leadSponsor.name` | 2026-11-10 | Match |

| 1 | Condition "Alzheimer's disease", total | matches | 4299 | 2026-11-10 | Match |
| 2 | Same search, Phase filter = Phase 3 | matches | Phase 3 (317) + Phase 2/3 (68) = 385 | 2026-11-10 | Match. The website's Phase 3 filter includes combined Phase 2/3 trials; this system gives combined phases their own bar so phase bars sum to the total |
| 3 | Condition "breast cancer", status Recruiting | matches | 2451 | 2026-11-10 | Match |
| 5b | NCT00065767 phase | Phase 2/3 | "Phase 2/3" (raw `["PHASE2","PHASE3"]`) | 2026-11-10 | Match |
