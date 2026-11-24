# AFRO-Form-A-data-workflow

Author: Gorgui Ba Toure (WHO AFRO GIS Centre)

Pipeline for the **Form A** ONA forms (vaccine management during a campaign: vaccine received, used, returned; one form per country and vaccine, "Form A for Type 2 vaccines" / "Formulaire A pour le nOPV2 / bOPV" / "Formulário A"), built like the team supervision / LQAS / IM pipelines: Python fetch, schema report, R processing,
quality control, push to the SharePoint *Data Repository* folder, data dictionary. **Config-driven**: one row per ONA form in
`config/forms.csv`, so a country or a new version of a form is added by editing that file.

## Status

| Step | State |
|---|---|
| `config/forms.csv` (every Form A form the token can see, one row each) | done |
| `fetch_ona_data.py` (submissions + the questions of each form) | done |
| `tools/inspect_forms.py` (which questions each country's form has, share filled, same-template groups) | done |
| processing, clean / standard geography, QC, dictionary, SharePoint push | built after the schema report has been read (the forms are in several languages and versions) |

## Run

```
pip install requests pandas pyarrow
python fetch_ona_data.py --definitions-only      # quick: only the questions of each form
python fetch_ona_data.py                         # submissions of every enabled form
python tools/inspect_forms.py                    # writes reports/form_templates.txt, form_schema_matrix.csv ...
Rscript run_pipeline.r                           # the same steps in one go
Rscript run_pipeline.r --countries ETH,TZA       # only some countries
```

## Processing (`process_form_a.py`)

Puts the 45 country forms (three versions of the form: `vials`, `daily`, `bottles`) in one table, one row per report of one place for one round.

- Questions are mapped to one standard name per figure (vials received, distributed, back from the lower level, physical stock, children vaccinated ...).
- Level of the report: National, Regional, District, Facility (sub-district and ward count as Facility). Names are kept as reported; a flag says when names below the level are filled.
- Campaign and round follow `data/lookup/campaign_lookup.xlsx` (the GPEI campaign lookup, copy of the admin data lookup): the typed name is matched to the lookup's OBR Name; when it is empty or not a campaign name it is found from country, vaccine and report date. The typed name is kept in `response_reported`.
- Repeated submissions of the same place, campaign, round and level within 75 days: the latest is kept. Further apart = another round, kept.
- Unaccounted vials, doses used, wastage rate and the theoretical stock are recomputed with one formula for every country.
- Names and phone numbers of people are not kept. `qc_flags` lists the checks a report fails; `reports/qc_summary.json` counts them.

Outputs: `data/final/afro_form_a_repository.csv` (not in git), `afro_form_a_country_round.csv`, `afro_form_a_district_round.csv` (facility reports only), `data/metadata/AFRO_form_a_dictionary.xlsx`.

Run: `python process_form_a.py` (about 35 seconds) or `Rscript run_pipeline.r --skip-fetch`.

## Tokens (never mixed)

| Country | Variable in `config/secrets.env` |
|---|---|
| Nigeria | `ONA_API_TOKEN_NIE` (no Nigeria Form A form was found with either token; add a row when its form id is known) |
| every other country | `ONA_API_TOKEN` |

Each row names its token in `token_env`; there is no fallback from one token to the other. Copy `config/secrets.env.example`
to `config/secrets.env` and fill it in (git-ignored; on Windows check it is not saved as `secrets.env.txt`).

## `config/forms.csv`

Columns: `form_id, country, iso3, name, variant, language, token_env, min_date, enabled, submissions_on_ona, last_submission, note`.
Built from the list of forms visible to `ONA_API_TOKEN` (`python tools/list_forms.py --token-env ONA_API_TOKEN`). `enabled` is
FALSE for forms with no submission. Several rows can share a country (a form per vaccine, an old and a new version, a
language): `variant` tells them apart. `note` flags what to confirm (CAE read as Cameroon, MAL read as Mali).

## Add or change a form

1. Add / edit a row in `config/forms.csv` (use `ONA_API_TOKEN` for any country other than Nigeria).
2. `python fetch_ona_data.py --form-ids <id>` then `python tools/inspect_forms.py`.
