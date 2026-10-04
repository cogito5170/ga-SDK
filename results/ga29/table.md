| mode | turns | claude -p runs | model calls | tool calls | tokens read (input+cache_read+cache_creation) | output tokens | cost USD | max context per call | passed checks |
|---|---|---|---|---|---|---|---|---|---|
| resume | 3 | 3 | 36 | 36 | 1,138,767 | 8,873 | 0.405 | 40,053 | yes |
| fresh | 3 | 3 | 41 | 42 | 1,118,889 | 11,528 | 0.238 | 32,566 | yes |
| fresh-small | 3 | 3 | 47 | 45 | 820,561 | 11,122 | 0.190 | 22,160 | yes |

Per hub turn (transcript file tN in the runner's own config dir, split at its turn.start events):

| mode | transcript, turn | model calls | first-call context | max context | tokens read |
|---|---|---|---|---|---|
| resume | t0 turn 1 | 13 | 23,260 | 27,457 | 330,836 |
| resume | t0 turn 2 | 10 | 29,173 | 33,002 | 313,947 |
| resume | t0 turn 3 | 13 | 34,637 | 40,053 | 493,984 |
| fresh | t0 turn 1 | 13 | 22,786 | 26,980 | 323,526 |
| fresh | t1 turn 1 | 13 | 23,417 | 29,539 | 353,475 |
| fresh | t2 turn 1 | 15 | 24,039 | 32,566 | 441,888 |
| fresh-small | t0 turn 1 | 15 | 14,179 | 19,496 | 257,272 |
| fresh-small | t1 turn 1 | 15 | 13,626 | 18,489 | 243,756 |
| fresh-small | t2 turn 1 | 17 | 14,621 | 22,160 | 319,533 |
