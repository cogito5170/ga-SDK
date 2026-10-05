# FINAL_TASK results (CMD-FT1 rev 2)

- valid runs: 100 of 100 planned (12 rev-1 T2 rows are void: kept in runs.jsonl, excluded from every table); `claude -p` calls: rev 1 107 (2 probes) + rev 2 106 of 130 = 213.
- cost: summed over all calls, quota_usd (from `usage`) $9.6013; quota_cli_usd (claude -p total_cost_usd) $18.3164 of the $35 cumulative cap (rev 1 $14.4854 + rev 2 $3.8310).
- stop: matrix complete.
- quota_usd = API list-price conversion of the `usage` object (Haiku $1/$5, Sonnet $2/$10 per Mtok; cache_creation x1.25, cache_read x0.1). quota_cli_usd = sum of `claude -p` total_cost_usd, priced from `modelUsage`, which also counts a small internal Haiku call per `claude -p` that `usage` omits (see ledger.jsonl model_usage). The real subscription weights are not public, so both are shown. Every ratio is a mean over the reps that ran; `[a-b]` is the range; a single run is shown without a range.

- claude -p `total_cost_usd` vs our arithmetic: 188 of 191 calls disagree by more than 5%.

## 1. Per arm x model x context

| arm | model | context | runs | accuracy | total_tokens | quota_usd | quota_cli_usd | max_call_input | quota_per_correct | cli_per_correct | repeated_information | llm_calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | Haiku | bulk (1 rep) | 5 | 5/5 | 334752 [332298-337392] | 0.4315 [0.4204-0.4435] | 1.0109 [0.9991-1.0234] | 110559 [110349-110898] | 0.4315 | 1.0109 | 4.2 [0.0-12.0] | 3.0 [3.0-3.0] |
| A | Sonnet | bulk (1 rep) | 5 | 4/5 | 393044 [392295-394798] | 0.9860 [0.9818-0.9937] | 1.7955 [1.6803-1.9165] | 131014 [130788-131501] | 1.2324 | 2.2444 | 3.2 [0.0-7.0] | 3.0 [3.0-3.0] |
| A | Haiku | selective | 15 | 14/15 | 7576 [5136-9752] | 0.0203 [0.0088-0.0303] | 0.0243 [0.0126-0.0340] | 1599 [1434-1853] | 0.0218 | 0.0261 | 3.7 [0.0-12.0] | 3.0 [3.0-3.0] |
| A | Sonnet | selective | 15 | 15/15 | 5833 [5296-7348] | 0.0145 [0.0022-0.0235] | 0.0253 [0.0060-0.0364] | 1975 [1842-2236] | 0.0145 | 0.0253 | 3.2 [0.0-7.0] | 3.0 [3.0-3.0] |
| B | Haiku | selective | 15 | 15/15 | 2876 [1514-6633] | 0.0085 [0.0026-0.0265] | 0.0098 [0.0029-0.0281] | 1383 [1211-1671] | 0.0085 | 0.0098 | 0.1 [0.0-1.0] | 1.1 [1.0-2.0] |
| B | Sonnet | selective | 15 | 15/15 | 1879 [1607-2351] | 0.0037 [0.0005-0.0077] | 0.0062 [0.0017-0.0125] | 1755 [1557-2107] | 0.0037 | 0.0062 | 0.0 [0.0-0.0] | 1.0 [1.0-1.0] |
| C | Haiku | selective | 15 | 15/15 | 3776 [1653-9771] | 0.0118 [0.0033-0.0422] | 0.0135 [0.0045-0.0438] | 1361 [1211-1671] | 0.0118 | 0.0135 | 0.3 [0.0-1.0] | 1.3 [1.0-2.0] |
| C | Sonnet | selective | 15 | 15/15 | 2408 [1607-3750] | 0.0037 [0.0005-0.0102] | 0.0064 [0.0005-0.0155] | 1729 [1557-2107] | 0.0037 | 0.0064 | 0.3 [0.0-1.0] | 1.3 [1.0-2.0] |

- Valid runs only: quota_usd $8.0263; quota_cli_usd $15.3155 (x1.91). The CLI figure is higher, most for the cheap selective calls (the internal Haiku call is a fixed overhead per `claude -p`).

## 2. Per task (selective)

| arm | model | task | runs | correct | quota_usd | quota_cli_usd | total_tokens | calls | tool/peer |
|---|---|---|---|---|---|---|---|---|---|
| A | Haiku | T1 | 3 | 3/3 | 0.0095 [0.0088-0.0101] | 0.0133 [0.0126-0.0139] | 5259 [5136-5368] | 3.0 [3.0-3.0] | 0/0 |
| A | Sonnet | T1 | 3 | 3/3 | 0.0105 [0.0022-0.0147] | 0.0197 [0.0060-0.0266] | 5519 [5517-5523] | 3.0 [3.0-3.0] | 0/0 |
| A | Haiku | T2 | 3 | 3/3 | 0.0205 [0.0158-0.0279] | 0.0250 [0.0203-0.0324] | 7943 [7014-9387] | 3.0 [3.0-3.0] | 0/0 |
| A | Sonnet | T2 | 3 | 3/3 | 0.0217 [0.0188-0.0235] | 0.0336 [0.0298-0.0364] | 7015 [6842-7348] | 3.0 [3.0-3.0] | 0/0 |
| A | Haiku | T3 | 3 | 3/3 | 0.0277 [0.0253-0.0294] | 0.0321 [0.0299-0.0339] | 9327 [8985-9752] | 3.0 [3.0-3.0] | 0/8 |
| A | Sonnet | T3 | 3 | 3/3 | 0.0144 [0.0113-0.0161] | 0.0255 [0.0210-0.0283] | 5832 [5780-5891] | 3.0 [3.0-3.0] | 0/0 |
| A | Haiku | T4 | 3 | 2/3 | 0.0250 [0.0148-0.0303] | 0.0287 [0.0185-0.0340] | 8298 [6240-9328] | 3.0 [3.0-3.0] | 1/0 |
| A | Sonnet | T4 | 3 | 3/3 | 0.0132 [0.0105-0.0147] | 0.0242 [0.0197-0.0266] | 5501 [5478-5526] | 3.0 [3.0-3.0] | 0/0 |
| A | Haiku | T5 | 3 | 3/3 | 0.0189 [0.0142-0.0251] | 0.0226 [0.0180-0.0290] | 7054 [6126-8333] | 3.0 [3.0-3.0] | 2/0 |
| A | Sonnet | T5 | 3 | 3/3 | 0.0128 [0.0102-0.0141] | 0.0234 [0.0190-0.0255] | 5296 [5296-5296] | 3.0 [3.0-3.0] | 0/0 |
| B | Haiku | T1 | 3 | 3/3 | 0.0030 [0.0029-0.0031] | 0.0039 [0.0029-0.0044] | 1733 [1698-1754] | 1.0 [1.0-1.0] | 0/0 |
| B | Sonnet | T1 | 3 | 3/3 | 0.0032 [0.0005-0.0046] | 0.0062 [0.0018-0.0085] | 1790 [1788-1791] | 1.0 [1.0-1.0] | 0/0 |
| B | Haiku | T2 | 3 | 3/3 | 0.0196 [0.0087-0.0265] | 0.0213 [0.0103-0.0281] | 5263 [3068-6633] | 1.0 [1.0-1.0] | 0/0 |
| B | Sonnet | T2 | 3 | 3/3 | 0.0045 [0.0029-0.0077] | 0.0072 [0.0045-0.0125] | 2351 [2350-2351] | 1.0 [1.0-1.0] | 0/0 |
| B | Haiku | T3 | 3 | 3/3 | 0.0056 [0.0051-0.0060] | 0.0069 [0.0064-0.0073] | 2206 [2121-2280] | 1.0 [1.0-1.0] | 0/0 |
| B | Sonnet | T3 | 3 | 3/3 | 0.0032 [0.0005-0.0045] | 0.0058 [0.0018-0.0084] | 1757 [1755-1758] | 1.0 [1.0-1.0] | 0/0 |
| B | Haiku | T4 | 3 | 3/3 | 0.0031 [0.0026-0.0040] | 0.0043 [0.0038-0.0052] | 1615 [1514-1797] | 1.0 [1.0-1.0] | 0/0 |
| B | Sonnet | T4 | 3 | 3/3 | 0.0029 [0.0005-0.0041] | 0.0053 [0.0017-0.0077] | 1609 [1607-1610] | 1.0 [1.0-1.0] | 0/0 |
| B | Haiku | T5 | 3 | 3/3 | 0.0113 [0.0095-0.0142] | 0.0129 [0.0107-0.0153] | 3565 [2873-4016] | 1.3 [1.0-2.0] | 1/0 |
| B | Sonnet | T5 | 3 | 3/3 | 0.0048 [0.0032-0.0074] | 0.0068 [0.0044-0.0109] | 1890 [1849-1913] | 1.0 [1.0-1.0] | 0/0 |
| C | Haiku | T1 | 3 | 3/3 | 0.0056 [0.0055-0.0058] | 0.0079 [0.0078-0.0081] | 3163 [3122-3201] | 2.0 [2.0-2.0] | 0/6 |
| C | Sonnet | T1 | 3 | 3/3 | 0.0059 [0.0010-0.0084] | 0.0114 [0.0033-0.0155] | 3246 [3242-3248] | 2.0 [2.0-2.0] | 0/6 |
| C | Haiku | T2 | 3 | 3/3 | 0.0284 [0.0200-0.0422] | 0.0300 [0.0217-0.0438] | 7008 [5341-9771] | 1.0 [1.0-1.0] | 0/0 |
| C | Sonnet | T2 | 3 | 3/3 | 0.0028 [0.0028-0.0028] | 0.0044 [0.0044-0.0044] | 2342 [2340-2344] | 1.0 [1.0-1.0] | 0/0 |
| C | Haiku | T3 | 3 | 3/3 | 0.0066 [0.0050-0.0083] | 0.0079 [0.0063-0.0097] | 2403 [2095-2759] | 1.0 [1.0-1.0] | 0/12 |
| C | Sonnet | T3 | 3 | 3/3 | 0.0005 [0.0005-0.0005] | 0.0014 [0.0005-0.0018] | 1757 [1755-1758] | 1.0 [1.0-1.0] | 0/12 |
| C | Haiku | T4 | 3 | 3/3 | 0.0041 [0.0033-0.0053] | 0.0053 [0.0045-0.0064] | 1823 [1653-2049] | 1.0 [1.0-1.0] | 0/0 |
| C | Sonnet | T4 | 3 | 3/3 | 0.0005 [0.0005-0.0005] | 0.0017 [0.0017-0.0017] | 1609 [1607-1610] | 1.0 [1.0-1.0] | 0/0 |
| C | Haiku | T5 | 3 | 3/3 | 0.0142 [0.0093-0.0208] | 0.0162 [0.0104-0.0232] | 4483 [2819-6124] | 1.7 [1.0-2.0] | 0/4 |
| C | Sonnet | T5 | 3 | 3/3 | 0.0088 [0.0067-0.0102] | 0.0132 [0.0102-0.0150] | 3087 [1833-3750] | 1.7 [1.0-2.0] | 2/0 |

## 3. H1 bulk vs selective (arm A, same model, same task; bulk is 1 run per task)

| model | pairs | quota_usd bulk/selective | quota_cli_usd bulk/selective | max_call_input bulk/selective | accuracy bulk | accuracy selective |
|---|---|---|---|---|---|---|
| Haiku | 5 | 24.7x ($2.1576 / $0.0872) | 47.3x ($5.0545 / $0.1069) | 70.6x (110559 / 1565) | 5/5 | 5/5 |
| Sonnet | 5 | 59.8x ($4.9298 / $0.0825) | 63.1x ($8.9776 / $0.1424) | 66.2x (131014 / 1980) | 4/5 | 5/5 |

## 4. H2 Sonnet vs Haiku, quota_per_correct (selective)

| task | arm | Haiku (correct/runs, usage $/correct, cli $/correct) | Sonnet (correct/runs, usage $/correct, cli $/correct) | Sonnet/Haiku usage | Sonnet/Haiku cli | cheaper per correct (usage / cli) |
|---|---|---|---|---|---|---|---|
| T1 | A | 3/3, 0.0095, 0.0133 | 3/3, 0.0105, 0.0197 | 1.11x | 1.49x | Haiku / Haiku |
| T1 | B | 3/3, 0.0030, 0.0039 | 3/3, 0.0032, 0.0062 | 1.07x | 1.62x | Haiku / Haiku |
| T1 | C | 3/3, 0.0056, 0.0079 | 3/3, 0.0059, 0.0114 | 1.04x | 1.44x | Haiku / Haiku |
| T1 | all | 9/9, 0.0061, 0.0084 | 9/9, 0.0065, 0.0125 | 1.08x | 1.49x | Haiku / Haiku |
| T2 | A | 3/3, 0.0205, 0.0250 | 3/3, 0.0217, 0.0336 | 1.06x | 1.34x | Haiku / Haiku |
| T2 | B | 3/3, 0.0196, 0.0213 | 3/3, 0.0045, 0.0072 | 0.23x | 0.34x | Sonnet / Sonnet |
| T2 | C | 3/3, 0.0284, 0.0300 | 3/3, 0.0028, 0.0044 | 0.10x | 0.15x | Sonnet / Sonnet |
| T2 | all | 9/9, 0.0228, 0.0254 | 9/9, 0.0097, 0.0150 | 0.42x | 0.59x | Sonnet / Sonnet |
| T3 | A | 3/3, 0.0277, 0.0321 | 3/3, 0.0144, 0.0255 | 0.52x | 0.80x | Sonnet / Sonnet |
| T3 | B | 3/3, 0.0056, 0.0069 | 3/3, 0.0032, 0.0058 | 0.57x | 0.84x | Sonnet / Sonnet |
| T3 | C | 3/3, 0.0066, 0.0079 | 3/3, 0.0005, 0.0014 | 0.08x | 0.18x | Sonnet / Sonnet |
| T3 | all | 9/9, 0.0133, 0.0156 | 9/9, 0.0060, 0.0109 | 0.45x | 0.70x | Sonnet / Sonnet |
| T4 | A | 2/3, 0.0375, 0.0431 | 3/3, 0.0132, 0.0242 | 0.35x | 0.56x | Sonnet / Sonnet |
| T4 | B | 3/3, 0.0031, 0.0043 | 3/3, 0.0029, 0.0053 | 0.94x | 1.24x | Sonnet / Haiku |
| T4 | C | 3/3, 0.0041, 0.0053 | 3/3, 0.0005, 0.0017 | 0.11x | 0.31x | Sonnet / Sonnet |
| T4 | all | 8/9, 0.0121, 0.0144 | 9/9, 0.0055, 0.0104 | 0.46x | 0.72x | Sonnet / Sonnet |
| T5 | A | 3/3, 0.0189, 0.0226 | 3/3, 0.0128, 0.0234 | 0.68x | 1.03x | Sonnet / Haiku |
| T5 | B | 3/3, 0.0113, 0.0129 | 3/3, 0.0048, 0.0068 | 0.43x | 0.53x | Sonnet / Sonnet |
| T5 | C | 3/3, 0.0142, 0.0162 | 3/3, 0.0088, 0.0132 | 0.62x | 0.81x | Sonnet / Sonnet |
| T5 | all | 9/9, 0.0148, 0.0172 | 9/9, 0.0088, 0.0144 | 0.59x | 0.84x | Sonnet / Sonnet |

## 5. H3 C vs A, FINAL_TASK section 4 criteria per model (selective)

### Haiku
- quota per correct, C vs A on the same cells: usage $0.0118 vs $0.0218; cli $0.0135 vs $0.0261 (information, not a section 4 criterion).
- compared on 15 (task, rep) cells that both arms ran (A 15 runs, C 15 runs).
- tokens_per_correct C <= A and accuracy C >= A: **PASS** (tokens_per_correct C 3776 vs A 8117; accuracy C 15/15 vs A 14/15)
- T4 tool_calls = peer_messages = 0 and correct, T5 UNKNOWN: **PASS** (C T4: 3/3; C T5 UNKNOWN: 3/3)
- repeated_information C < A: **PASS** (mean C 0.33 vs A 3.73)
- spec 15 (connectivity and message count are not evidence of intelligence): C's peer messages are reported per run, not scored as a merit.

### Sonnet
- quota per correct, C vs A on the same cells: usage $0.0037 vs $0.0145; cli $0.0064 vs $0.0253 (information, not a section 4 criterion).
- compared on 15 (task, rep) cells that both arms ran (A 15 runs, C 15 runs).
- tokens_per_correct C <= A and accuracy C >= A: **PASS** (tokens_per_correct C 2408 vs A 5833; accuracy C 15/15 vs A 15/15)
- T4 tool_calls = peer_messages = 0 and correct, T5 UNKNOWN: **PASS** (C T4: 3/3; C T5 UNKNOWN: 3/3)
- repeated_information C < A: **PASS** (mean C 0.33 vs A 3.20)
- spec 15 (connectivity and message count are not evidence of intelligence): C's peer messages are reported per run, not scored as a merit.

## 6. Which quota measure bound each conclusion

- H1 Haiku: bulk/selective x24.7 by usage, x47.3 by cli; direction agrees; the smaller ratio (the conservative one) is the one that binds the claim.
- H1 Sonnet: bulk/selective x59.8 by usage, x63.1 by cli; direction agrees; the smaller ratio (the conservative one) is the one that binds the claim.
- H2: 20 (task, arm) cells where both models got something right; the cheaper-per-correct model differs between usage and cli in 2 (T4/B, T5/A); those verdicts are bound by the measure chosen and are marked as such.
- Which one is the real subscription quota is not public; usage is the list-price model of what the call is, cli is what `claude -p` says it cost including its own overhead. Where they agree the conclusion stands on both; where they differ it is stated for each.
