<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Synthetic RTMS fixtures

All records are invented. Only XML structure and field names follow the bounded
provider probe; no real transaction records were copied. Success is `response /
header / resultCode = 000`, with `body / items / item`, `numOfRows`, `pageNo`,
`totalCount`. Both provider and gateway HTTP-200 error envelopes are represented.

| Provider field | Normalized mapping |
|---|---|
| dealAmount | source_price; price_krw = integer 만원 × 10000 (sale) |
| deposit / monthlyRent | separate deposit_krw / monthly_rent_krw |
| preDeposit / preMonthlyRent | separate previous_*_krw |
| contractType / contractTerm | new_or_renewal / contract_term; no inferred dates |
| excluUseAr | area_m2, Decimal; area_type exclusive |
| floor / buildYear | integer floor / build_year |
| dealYear / dealMonth / dealDay | contract_date; incomplete dates rejected |
| cdealType / cdealDay | raw cancellation_status / date (YY.MM.DD, 2000–2099) |
| aptNm / aptSeq | building_name / provider_record_id |
| umdNm + jibun | address_original, partial visibility |
| sggCd | retained in extra; lawd_code is the query's five-digit code |
| aptDong, buyerGbn, slerGbn, dealingGbn, estateAgentSggNm | extra |
| landLeaseholdGbn, rgstDate, roadnm*, useRRRight, unknown fields | extra |

Blank or whitespace-only fields normalize to None, never zero. Original monetary
strings remain in sanitized XML pages, including rental amounts. aptSeq identifies
a provider apartment, not necessarily a unique transaction: no deduplication or
exact property matching follows from it. Fingerprints never delete records.

`zero.xml` has a valid explicit zero total; the two sale-page fixtures form a
complete two-page response. `cancelled.xml` uses O and 25.11.03.
`malformed-number.xml` intentionally contains invalid comma grouping.
