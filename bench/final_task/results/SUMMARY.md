# FINAL_TASK results (CMD-FT1)

- runs recorded: 58 of 100 planned; `claude -p` calls in the ledger: 107 of 110 (2 were availability probes); summed quota_usd: $7.5003 of $35.
- stop: cap: 107 of 110 claude -p calls used (the next run could pass 110); $7.50 of $35.
- quota_usd = API list-price conversion of the `usage` object (Haiku $1/$5, Sonnet $2/$10 per Mtok; cache_creation x1.25, cache_read x0.1); the real subscription weights are not public. Every ratio is a mean over the reps that ran; `[a-b]` is the range; a single run is shown without a range.

- claude -p `total_cost_usd` vs our arithmetic: 103 of 105 calls disagree by more than 5%.

## 1. Per arm x model x context

| arm | model | context | runs | accuracy | total_tokens | quota_usd | max_call_input | quota_per_correct | repeated_information | llm_calls |
|---|---|---|---|---|---|---|---|---|---|---|
| A | Haiku | bulk (1 rep) | 5 | 4/5 | 334416 [332298-337392] | 0.4304 [0.4204-0.4435] | 110508 [110349-110794] | 0.5380 | 4.2 [0.0-12.0] | 3.0 [3.0-3.0] |
| A | Sonnet | bulk (1 rep) | 5 | 3/5 | 392859 [392295-393871] | 0.9857 [0.9818-0.9922] | 130944 [130788-131151] | 1.6428 | 3.2 [0.0-7.0] | 3.0 [3.0-3.0] |
| A | Haiku | selective | 5 | 4/5 | 7430 [5136-9828] | 0.0199 [0.0088-0.0300] | 1568 [1446-1777] | 0.0248 | 3.0 [0.0-6.0] | 3.0 [3.0-3.0] |
| A | Sonnet | selective | 5 | 4/5 | 5816 [5296-6845] | 0.0165 [0.0141-0.0227] | 1981 [1842-2231] | 0.0206 | 3.2 [0.0-7.0] | 3.0 [3.0-3.0] |
| B | Haiku | selective | 10 | 8/10 | 3044 [1514-6598] | 0.0092 [0.0026-0.0263] | 1384 [1217-1663] | 0.0115 | 0.1 [0.0-1.0] | 1.1 [1.0-2.0] |
| B | Sonnet | selective | 10 | 8/10 | 1881 [1610-2338] | 0.0037 [0.0005-0.0077] | 1753 [1560-2093] | 0.0046 | 0.0 [0.0-0.0] | 1.0 [1.0-1.0] |
| C | Haiku | selective | 9 | 7/9 | 3141 [1768-5105] | 0.0085 [0.0039-0.0189] | 1376 [1247-1663] | 0.0110 | 0.3 [0.0-1.0] | 1.3 [1.0-2.0] |
| C | Sonnet | selective | 9 | 7/9 | 2404 [1610-3750] | 0.0030 [0.0005-0.0102] | 1745 [1595-2093] | 0.0039 | 0.3 [0.0-1.0] | 1.3 [1.0-2.0] |

## 1b. Same, T2 excluded (T2 is void: its fixture test file imports only the old names, so no answer's test could call slugify; see STATE.md)

| arm | model | context | runs | accuracy | total_tokens | quota_usd | max_call_input | quota_per_correct | repeated_information | llm_calls |
|---|---|---|---|---|---|---|---|---|---|---|
| A | Haiku | bulk (1 rep) | 4 | 4/4 | 334353 [332298-337392] | 0.4305 [0.4204-0.4435] | 110475 [110349-110794] | 0.4305 | 5.2 [2.0-12.0] | 3.0 [3.0-3.0] |
| A | Sonnet | bulk (1 rep) | 4 | 3/4 | 392606 [392295-393405] | 0.9840 [0.9818-0.9894] | 130892 [130788-131113] | 1.3120 | 4.0 [2.0-7.0] | 3.0 [3.0-3.0] |
| A | Haiku | selective | 4 | 4/4 | 6831 [5136-9244] | 0.0173 [0.0088-0.0284] | 1516 [1446-1691] | 0.0173 | 3.8 [2.0-6.0] | 3.0 [3.0-3.0] |
| A | Sonnet | selective | 4 | 4/4 | 5559 [5296-5891] | 0.0149 [0.0141-0.0161] | 1918 [1842-2055] | 0.0149 | 4.0 [2.0-7.0] | 3.0 [3.0-3.0] |
| B | Haiku | selective | 8 | 8/8 | 2333 [1514-4016] | 0.0058 [0.0026-0.0142] | 1314 [1217-1410] | 0.0058 | 0.1 [0.0-1.0] | 1.1 [1.0-2.0] |
| B | Sonnet | selective | 8 | 8/8 | 1767 [1610-1913] | 0.0032 [0.0005-0.0074] | 1668 [1560-1775] | 0.0032 | 0.0 [0.0-0.0] | 1.0 [1.0-1.0] |
| C | Haiku | selective | 7 | 7/7 | 2734 [1768-4505] | 0.0064 [0.0039-0.0127] | 1294 [1247-1368] | 0.0064 | 0.4 [0.0-1.0] | 1.4 [1.0-2.0] |
| C | Sonnet | selective | 7 | 7/7 | 2426 [1610-3750] | 0.0031 [0.0005-0.0102] | 1645 [1595-1742] | 0.0031 | 0.4 [0.0-1.0] | 1.4 [1.0-2.0] |

- Sum over runs: quota_usd from `usage` $7.4943; claude -p `total_cost_usd` $14.4775. total_cost_usd is priced from `modelUsage`, which also counts a small internal Haiku call per `claude -p` that `usage` does not list (see ledger.jsonl model_usage); so claude -p's own figure is higher, most for the cheap selective calls.

## 2. Per task (selective)

| arm | model | task | runs | correct | quota_usd | total_tokens | calls | tool/peer |
|---|---|---|---|---|---|---|---|---|
| A | Haiku | T1 | 1 | 1/1 | 0.0088 | 5136 | 3.0 | 0/0 |
| A | Sonnet | T1 | 1 | 1/1 | 0.0147 | 5523 | 3.0 | 0/0 |
| A | Haiku | T2 | 1 | 0/1 | 0.0300 | 9828 | 3.0 | 0/0 |
| A | Sonnet | T2 | 1 | 0/1 | 0.0227 | 6845 | 3.0 | 0/0 |
| A | Haiku | T3 | 1 | 1/1 | 0.0284 | 9244 | 3.0 | 0/0 |
| A | Sonnet | T3 | 1 | 1/1 | 0.0161 | 5891 | 3.0 | 0/0 |
| A | Haiku | T4 | 1 | 1/1 | 0.0148 | 6240 | 3.0 | 0/0 |
| A | Sonnet | T4 | 1 | 1/1 | 0.0147 | 5526 | 3.0 | 0/0 |
| A | Haiku | T5 | 1 | 1/1 | 0.0173 | 6704 | 3.0 | 0/0 |
| A | Sonnet | T5 | 1 | 1/1 | 0.0141 | 5296 | 3.0 | 0/0 |
| B | Haiku | T1 | 2 | 2/2 | 0.0030 [0.0029-0.0031] | 1726 [1698-1754] | 1.0 [1.0-1.0] | 0/0 |
| B | Sonnet | T1 | 2 | 2/2 | 0.0026 [0.0005-0.0046] | 1791 [1791-1791] | 1.0 [1.0-1.0] | 0/0 |
| B | Haiku | T2 | 2 | 0/2 | 0.0228 [0.0192-0.0263] | 5888 [5178-6598] | 1.0 [1.0-1.0] | 0/0 |
| B | Sonnet | T2 | 2 | 0/2 | 0.0053 [0.0029-0.0077] | 2338 [2337-2338] | 1.0 [1.0-1.0] | 0/0 |
| B | Haiku | T3 | 2 | 2/2 | 0.0054 [0.0051-0.0056] | 2170 [2121-2218] | 1.0 [1.0-1.0] | 0/0 |
| B | Sonnet | T3 | 2 | 2/2 | 0.0025 [0.0005-0.0045] | 1758 [1758-1758] | 1.0 [1.0-1.0] | 0/0 |
| B | Haiku | T4 | 2 | 2/2 | 0.0026 [0.0026-0.0027] | 1524 [1514-1534] | 1.0 [1.0-1.0] | 0/0 |
| B | Sonnet | T4 | 2 | 2/2 | 0.0023 [0.0005-0.0041] | 1610 [1610-1610] | 1.0 [1.0-1.0] | 0/0 |
| B | Haiku | T5 | 2 | 2/2 | 0.0122 [0.0102-0.0142] | 3911 [3806-4016] | 1.5 [1.0-2.0] | 1/0 |
| B | Sonnet | T5 | 2 | 2/2 | 0.0056 [0.0038-0.0074] | 1910 [1907-1913] | 1.0 [1.0-1.0] | 0/0 |
| C | Haiku | T1 | 2 | 2/2 | 0.0057 [0.0056-0.0058] | 3184 [3166-3201] | 2.0 [2.0-2.0] | 0/4 |
| C | Sonnet | T1 | 2 | 2/2 | 0.0047 [0.0010-0.0084] | 3248 [3248-3248] | 2.0 [2.0-2.0] | 0/4 |
| C | Haiku | T2 | 2 | 0/2 | 0.0162 [0.0135-0.0189] | 4564 [4023-5105] | 1.0 [1.0-1.0] | 0/0 |
| C | Sonnet | T2 | 2 | 0/2 | 0.0028 [0.0028-0.0028] | 2329 [2328-2330] | 1.0 [1.0-1.0] | 0/0 |
| C | Haiku | T3 | 2 | 2/2 | 0.0057 [0.0050-0.0063] | 2226 [2095-2356] | 1.0 [1.0-1.0] | 0/8 |
| C | Sonnet | T3 | 2 | 2/2 | 0.0005 [0.0005-0.0005] | 1758 [1758-1758] | 1.0 [1.0-1.0] | 0/8 |
| C | Haiku | T4 | 2 | 2/2 | 0.0046 [0.0039-0.0053] | 1908 [1768-2049] | 1.0 [1.0-1.0] | 0/0 |
| C | Sonnet | T4 | 2 | 2/2 | 0.0005 [0.0005-0.0005] | 1610 [1610-1610] | 1.0 [1.0-1.0] | 0/0 |
| C | Haiku | T5 | 1 | 1/1 | 0.0127 | 4505 | 2.0 | 0/2 |
| C | Sonnet | T5 | 1 | 1/1 | 0.0102 | 3750 | 2.0 | 1/0 |

## 3. H1 bulk vs selective (arm A, same model, same task; bulk is 1 run per task)

| model | pairs | quota_usd bulk/selective | max_call_input bulk/selective | accuracy bulk | accuracy selective |
|---|---|---|---|---|---|
| Haiku | 5 | 21.7x ($2.1520 / $0.0993) | 70.5x (110508 / 1568) | 4/5 | 4/5 |
| Sonnet | 5 | 59.9x ($4.9283 / $0.0823) | 66.1x (130944 / 1981) | 3/5 | 4/5 |

## 4. H2 Sonnet vs Haiku, quota_per_correct (selective)

| task | arm | Haiku (correct/runs, $/correct) | Sonnet (correct/runs, $/correct) | Sonnet/Haiku | cheaper per correct |
|---|---|---|---|---|---|
| T1 | A | 1/1, 0.0088 | 1/1, 0.0147 | 1.67x | Haiku |
| T1 | B | 2/2, 0.0030 | 2/2, 0.0026 | 0.86x | Sonnet |
| T1 | C | 2/2, 0.0057 | 2/2, 0.0047 | 0.81x | Sonnet |
| T1 | all | 5/5, 0.0052 | 5/5, 0.0058 | 1.11x | Haiku |
| T2 | A | 0/1, n/a | 0/1, n/a | n/a | - |
| T2 | B | 0/2, n/a | 0/2, n/a | n/a | - |
| T2 | C | 0/2, n/a | 0/2, n/a | n/a | - |
| T2 | all | 0/5, n/a | 0/5, n/a | n/a | - |
| T3 | A | 1/1, 0.0284 | 1/1, 0.0161 | 0.57x | Sonnet |
| T3 | B | 2/2, 0.0054 | 2/2, 0.0025 | 0.47x | Sonnet |
| T3 | C | 2/2, 0.0057 | 2/2, 0.0005 | 0.09x | Sonnet |
| T3 | all | 5/5, 0.0101 | 5/5, 0.0044 | 0.44x | Sonnet |
| T4 | A | 1/1, 0.0148 | 1/1, 0.0147 | 0.99x | Sonnet |
| T4 | B | 2/2, 0.0026 | 2/2, 0.0023 | 0.88x | Sonnet |
| T4 | C | 2/2, 0.0046 | 2/2, 0.0005 | 0.10x | Sonnet |
| T4 | all | 5/5, 0.0058 | 5/5, 0.0041 | 0.69x | Sonnet |
| T5 | A | 1/1, 0.0173 | 1/1, 0.0141 | 0.81x | Sonnet |
| T5 | B | 2/2, 0.0122 | 2/2, 0.0056 | 0.46x | Sonnet |
| T5 | C | 1/1, 0.0127 | 1/1, 0.0102 | 0.81x | Sonnet |
| T5 | all | 4/4, 0.0136 | 4/4, 0.0089 | 0.65x | Sonnet |

## 5. H3 C vs A, FINAL_TASK section 4 criteria per model (selective)

### Haiku
- compared on 5 (task, rep) cells that both arms ran (A 5 runs, C 5 runs).
- tokens_per_correct C <= A and accuracy C >= A: **PASS** (tokens_per_correct C 3898 vs A 9288; accuracy C 4/5 vs A 4/5)
- T4 tool_calls = peer_messages = 0 and correct, T5 UNKNOWN: **PASS** (C T4: 2/2; C T5 UNKNOWN: 1/1)
- repeated_information C < A: **PASS** (mean C 0.40 vs A 3.00)
- spec 15 (connectivity and message count are not evidence of intelligence): C's peer messages are reported per run, not scored as a merit.

### Sonnet
- compared on 5 (task, rep) cells that both arms ran (A 5 runs, C 5 runs).
- tokens_per_correct C <= A and accuracy C >= A: **PASS** (tokens_per_correct C 3174 vs A 7270; accuracy C 4/5 vs A 4/5)
- T4 tool_calls = peer_messages = 0 and correct, T5 UNKNOWN: **PASS** (C T4: 2/2; C T5 UNKNOWN: 1/1)
- repeated_information C < A: **PASS** (mean C 0.40 vs A 3.20)
- spec 15 (connectivity and message count are not evidence of intelligence): C's peer messages are reported per run, not scored as a merit.

