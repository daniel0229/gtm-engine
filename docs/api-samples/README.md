# API samples

Real responses from the two public Polish registries the enrichment step uses. Captured 2026-11-12, no API key. The test subject is ORLEN S.A. (NIP 7740001454, KRS 0000028860), a large public company, chosen only because its identifiers are widely known.

## VAT whitelist (Ministry of Finance)

Base URL: `https://wl-api.mf.gov.pl`. The `date` parameter (YYYY-MM-DD) is required.

| File | Request | Result |
|---|---|---|
| `vat-whitelist-nip.json` | `GET /api/search/nip/7740001454?date=2026-11-12` | 200, one subject |
| `vat-whitelist-nips-batch.json` | `GET /api/search/nips/7740001454,5260250995?date=2026-11-12` | 200, two entries in one call |
| `vat-whitelist-error-invalid-nip.json` | `GET /api/search/nip/1234567890?date=…` | 400, `WL-115` "Nieprawidłowy NIP." |

Fields we use: `name`, `nip`, `regon`, `krs`, `statusVat` ("Czynny" = active), `residenceAddress`, `workingAddress`, `registrationLegalDate`. The response also lists every registered bank account number (about 100 for this company); we don't store them.

The responses carry no rate-limit headers. The daily limit and the maximum number of NIPs per batch call still have to be confirmed in the official docs (step 1.1).

## KRS (Ministry of Justice)

Base URL: `https://api-krs.ms.gov.pl`.

| File | Request | Result |
|---|---|---|
| `krs-odpis-aktualny.json` | `GET /api/krs/OdpisAktualny/0000028860?rejestr=P&format=json` | 200, current extract |
| `krs-error-not-found.json` | `GET /api/krs/OdpisAktualny/0000000001?rejestr=P&format=json` | 404 |

`rejestr=P` = register of businesses. Useful paths:

- `odpis.dane.dzial1.danePodmiotu.formaPrawna`: legal form
- `odpis.naglowekA.dataRejestracjiWKRS`: date of entry into KRS
- `odpis.dane.dzial1.siedzibaIAdres`: registered address
- `odpis.dane.dzial2.reprezentacja.sklad[]`: management board, with `funkcjaWOrganie` (e.g. "PREZES ZARZĄDU")

**The API masks people's names.** Board members come back as `"nazwisko": "F*****"` and `"imie": "I*******"`, and PESEL is masked the same way. The API gives the board's size and roles, not who is on it.
