#!/usr/bin/env python3
"""
process_form_a.py   (step 2 of run_pipeline.r)
Author: Gorgui Ba Toure

Form A = the end-of-round vaccine accountability form: every level (national, region, district, health facility) reports how many
vials it had, received, distributed, got back, and how many children were vaccinated. One record = one report of one place for one
round. This step puts the 45 country forms (several versions and languages) in ONE table.

What it does
  1  reads data/raw/<iso3>_<form_id>.parquet of every enabled row of config/forms.csv
  2  gives each question one standard name (the three versions of the form: "vials", "daily" and "bottles", see FIGURES)
  3  cleans text, numbers, dates, the level of the report, the round (Rnd1, Rnd2 ...) and the campaign name
     (the lookup is the authority: Response = OBR Name, round = the lookup's round, as in the admin data)
  4  removes repeated submissions (same place, campaign, round and level within 75 days: the latest is kept)
  5  recomputes the stock arithmetic of the form the same way for every country (unaccounted vials, doses used, wastage, theoretical stock)
  6  flags what looks wrong (qc_flags) and writes the repository, two summary tables, the quality summary and the dictionary

Outputs (data/ is git-ignored except the two small summary tables and the dictionary)
  data/final/afro_form_a_repository.csv           one row per report (cleaned)            local + SharePoint
  data/final/afro_form_a_country_round.csv        country x campaign x round x level      git + SharePoint
  data/final/afro_form_a_district_round.csv       district x campaign x round (facility-level reports only: no double counting)
  data/metadata/AFRO_form_a_dictionary.xlsx       data dictionary (columns, forms, rules)
  reports/qc_summary.json, reports/form_a_unmatched_campaigns.csv

Usage:  python process_form_a.py        (run from the project folder)
"""
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RAW = HERE / "data" / "raw"
FINAL = HERE / "data" / "final"
META = HERE / "data" / "metadata"
REP = HERE / "reports"
LOOKUP_CANDIDATES = [HERE / "data" / "lookup" / "campaign_lookup.xlsx", HERE / "data" / "lookup" / "lookup.xlsx"]

# ------------------------------------------------------------------------------------------------ standard names
# standard name -> the question names that carry it, in the three versions of the form
#   vials    (2025-2026, 40 forms): usable_vials_*
#   daily    (older Burkina / Chad / CAR / Niger / Ghana): *_day, *_returned_team
#   bottles  (oldest): bottles_*
FIGURES = {
    "vials_start_stock":        ["usable_vials_stock_beginning", "usable_vials_start_day", "bottles_start_Stock"],
    "vials_received":           ["usable_vials_received", "vials_received", "bottles_received"],
    "vials_replenished":        ["usable_vials_replenish", "usable_vials_replenish_day", "bottles_replenish"],
    "vials_transferred":        ["usable_transfered"],
    "vials_distributed":        ["usable_vials_distributed", "bottles_distributed"],
    "usable_back_from_lower":   ["usable_vials_coming_back", "usable_vials_returned_team", "bottles_unopened_reusable"],
    "unusable_back_from_lower": ["unusable_vials_coming_back", "unusable_vials_returned_team", "bottles_returned_open"],
    "usable_physical":          ["usable_vials_physical", "bottles_usable_physical", "phys_usable"],
    "unusable_physical":        ["unusable_vials_physical", "bottles_UNusable_physical", "phys_unusable"],
    "usable_returned_higher":   ["total_returned", "total_vials_returned", "bottles_usable_Returned"],
    "unusable_returned_higher": ["bottles_UNusable_Returned"],
    "unusable_disposed":        ["unusable_vials_disposed", "bottles_UNusable_disposed"],
    "children_vaccinated":      ["vaccinated_children", "children_vaccinated", "Vaccinat_child", "TotChild_vaccinated", "Chldren-Vaccinated_FormA"],
    "doses_per_vial":           ["doses_per_vial"],
    "target_population":        ["target_population"],
}
VERSION_MARKERS = {"vials": "usable_vials_distributed", "daily": "Campaign_Day", "bottles": "bottles_distributed"}
TEXT_SOURCES = {
    "region_raw":   ["Region", "region"],
    "district_raw": ["District", "district", "DistHosp"],
    "facility_raw": ["facility", "Facility", "nameofhf", "facility_other", "facility_autre", "other_hf", "Sub_District", "Ward", "ward", "wards"],
    "response_raw": ["response_ID", "Response"],
    "round_raw":    ["roundNumber"],
    "level_raw":    ["Admin_LvL_Rpt", "Admin_Level"],
    "reverse_logistics_on_time": ["reverse_logistics"],
    "form_version": ["Version_ID"],
}
COMMENT_COLS = ["VAM_comments"]            # free text kept; names / phone numbers of people are NOT kept
MAX_NUMBER = 5_000_000                     # a count of vials / children above this is a typing error
DOSES_OK = {1, 2, 5, 10, 20, 25, 50, 100}
LEVEL_MAP = {"national": "National", "country": "National", "regional": "Regional", "region": "Regional", "state": "Regional", "province": "Regional",
             "district": "District", "county": "District", "sub-district": "Facility", "subdistrict": "Facility", "sub-county": "Facility",
             "subcounty": "Facility", "ward": "Facility", "facility": "Facility", "health facility": "Facility"}
LEVEL_ORDER = ["National", "Regional", "District", "Facility"]


def log(msg):
    print(f"[process_form_a] {msg}", flush=True)


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(c))


def key(s):
    """name for matching: no accents, lower case, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", strip_accents(s).lower()) if isinstance(s, str) else ""


def clean_name(s):
    """a place name the way the admin data writes it: capitals, single spaces, no underscores."""
    if not isinstance(s, str):
        return ""
    t = re.sub(r"[_\s]+", " ", s.strip())
    return t.upper()


def parse_dates(s, utc=False):
    """dates in ONA come in mixed forms (2021-05-06, 2021-05-06T11:26:22, with or without a zone, or 06/05/2021): read each one on its own."""
    t = pd.Series(s).astype(str).str.strip().replace({"nan": "", "None": "", "NaT": ""})
    a = pd.to_datetime(t, format="ISO8601", errors="coerce", utc=utc)
    miss = a.isna() & (t != "")
    if miss.any():
        b = pd.to_datetime(t[miss], format="mixed", dayfirst=True, errors="coerce", utc=utc)
        a = a.fillna(b)
    return a


def short_columns(df):
    """one column per question name (the last part of 'group/question', repeat indices removed); the first non-empty value wins."""
    groups = {}
    for c in df.columns:
        groups.setdefault(re.sub(r"\[\d+\]", "", str(c)).split("/")[-1], []).append(c)
    out = {}
    for name, cols in groups.items():
        s = df[cols[0]]
        for c in cols[1:]:
            s = s.where(s.astype(str).str.strip() != "", df[c])
        out[name] = s
    return pd.DataFrame(out)


def first_text(df, names):
    s = pd.Series("", index=df.index, dtype=object)
    for n in names:
        if n in df.columns:
            v = df[n].astype(str).str.strip().replace({"nan": "", "None": ""})
            s = s.where(s != "", v)
    return s


def first_number(df, names):
    s = pd.Series(np.nan, index=df.index, dtype=float)
    for n in names:
        if n in df.columns:
            s = s.fillna(pd.to_numeric(df[n].astype(str).str.replace(",", ".").str.strip(), errors="coerce"))
    return s


def norm_round(x):
    """Rnd1, RND 1, 'round 1', '1' -> Rnd1; anything else (a campaign name typed in the round question) -> ''."""
    if not isinstance(x, str):
        return ""
    t = x.strip()
    m = re.fullmatch(r"(?i)(?:rnd|round|rd|r|passage|tour)?\s*[-_]?\s*(\d{1,2})", t)
    return "Rnd%d" % int(m.group(1)) if m else ""


COUNTRY_ALIAS = {"tanzania": "unitedrepublicoftanzania", "unitedrepublicoftanzania": "unitedrepublicoftanzania", "drc": "democraticrepublicofthecongo"}


def ckey(s):
    k = key(s)
    return COUNTRY_ALIAS.get(k, k)


def read_lookup():
    for p in LOOKUP_CANDIDATES:
        if p.exists():
            L = pd.read_excel(p)
            L = L[L["OBR Name"].notna()].copy()
            L["_ck"] = L["Country"].map(ckey)
            L["_rk"] = L["OBR Name"].map(key)
            L["_d"] = pd.to_datetime(L["Round Start Date"], errors="coerce")
            L["_rn"] = L["Round Number"].map(lambda x: int(re.search(r"(\d+)", str(x)).group(1)) if re.search(r"(\d+)", str(x)) else np.nan)
            log(f"lookup: {p.name} ({len(L)} rounds)")
            return L
    log("lookup not found in data/lookup/: campaign names are only cleaned, not matched")
    return None


# ------------------------------------------------------------------------------------------------ load
def load_form(row):
    stem = f"{row['iso3']}_{row['form_id']}"
    pq = RAW / f"{stem}.parquet"
    if not pq.exists():
        return None
    raw = pd.read_parquet(pq)
    d = short_columns(raw)
    out = pd.DataFrame(index=d.index)
    out["record_uuid"] = d["_uuid"] if "_uuid" in d.columns else (d["uuid"] if "uuid" in d.columns else "")
    out["country"], out["iso3"], out["form_id"] = row["country"], row["iso3"], int(row["form_id"])
    out["form_name"], out["vaccine"], out["language"] = row["name"], row.get("variant", ""), row.get("language", "")
    template = [v for v, m in VERSION_MARKERS.items() if m in d.columns]
    out["template"] = template[0] if template else "other"
    out["submission_time"] = parse_dates(d["_submission_time"], utc=True) if "_submission_time" in d.columns else pd.NaT
    out["_start"] = parse_dates(d["starttime"], utc=True) if "starttime" in d.columns else pd.NaT
    out["_today"] = parse_dates(first_text(d, ["today"]))
    out["_form_date"] = parse_dates(first_text(d, ["Date_of_Reporting_Updates"]))
    out["campaign_start_reported"] = parse_dates(first_text(d, ["start_day"]))
    out["antigen_reported"] = first_text(d, ["antigen"])
    for new, names in TEXT_SOURCES.items():
        out[new] = first_text(d, names)
    for std, names in FIGURES.items():
        out[std] = first_number(d, names)
    out["form_wastage_rate"] = first_number(d, ["wastage_rate"])
    out["vam_comments"] = first_text(d, COMMENT_COLS)
    gps = first_text(d, ["GIS_Location"])
    xy = gps.str.extract(r"^\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)")
    out["latitude"], out["longitude"] = pd.to_numeric(xy[0], errors="coerce"), pd.to_numeric(xy[1], errors="coerce")
    out["min_date"] = pd.to_datetime(row.get("min_date") or "2019-01-01", errors="coerce")
    return out


# ------------------------------------------------------------------------------------------------ cleaning
def clean(df, lk):
    flags = pd.DataFrame(False, index=df.index, columns=[
        "implausible_number", "doses_per_vial_odd", "round_not_a_round", "date_from_submission", "level_inferred", "no_geography",
        "returned_exceeds_distributed", "returned_below_distributed", "geography_below_level", "stock_mismatch", "wastage_negative", "wastage_over_100",
        "no_figures", "campaign_not_in_lookup", "campaign_from_lookup_date"])
    # --- numbers
    num_cols = list(FIGURES)
    for c in num_cols:
        bad = df[c].notna() & ((df[c] < 0) | (df[c] > MAX_NUMBER))
        flags.loc[bad, "implausible_number"] = True
        df.loc[bad, c] = np.nan
    odd = df["doses_per_vial"].notna() & ~df["doses_per_vial"].isin(DOSES_OK)
    flags.loc[odd, "doses_per_vial_odd"] = True
    df.loc[odd, "doses_per_vial"] = np.nan
    figs = ["vials_start_stock", "vials_received", "vials_distributed", "usable_back_from_lower", "unusable_back_from_lower",
            "usable_physical", "children_vaccinated"]
    flags["no_figures"] = df[figs].isna().all(axis=1)
    # --- dates: the date typed in the form if plausible, else the submission date
    sub_d = df["submission_time"].dt.tz_convert(None).dt.normalize()
    cand = df["_form_date"].fillna(df["_today"])
    ok = cand.notna() & (cand >= df["min_date"]) & (cand <= sub_d + pd.Timedelta(days=1)) & (cand >= sub_d - pd.Timedelta(days=366))
    df["report_date"] = cand.where(ok, sub_d)
    df["date_source"] = np.where(ok, "form", "submission")
    flags["date_from_submission"] = ~ok
    df["report_year"] = df["report_date"].dt.year
    # --- level of the report
    lv = df["level_raw"].str.strip().str.lower().map(LEVEL_MAP)
    inferred = lv.isna()
    df["facility"] = df["facility_raw"].map(clean_name)
    df["district"] = df["district_raw"].map(clean_name)
    df["region"] = df["region_raw"].map(clean_name)
    guess = np.where(df["facility"] != "", "Facility", np.where(df["district"] != "", "District", np.where(df["region"] != "", "Regional", "National")))
    df["level"] = lv.where(~inferred, pd.Series(guess, index=df.index))
    flags["level_inferred"] = inferred
    # names are kept as reported, even when they are below the level (a "District" report that names a facility); the flag says so
    flags["geography_below_level"] = ((df["level"] == "National") & ((df["region"] != "") | (df["district"] != "") | (df["facility"] != ""))) | \
                                     ((df["level"] == "Regional") & ((df["district"] != "") | (df["facility"] != ""))) | \
                                     ((df["level"] == "District") & (df["facility"] != ""))
    flags["no_geography"] = ((df["level"] == "Regional") & (df["region"] == "")) | ((df["level"] == "District") & (df["district"] == "")) | \
                            ((df["level"] == "Facility") & (df["facility"] == ""))
    # --- campaign and round
    df["round"] = df["round_raw"].map(norm_round)
    flags["round_not_a_round"] = (df["round_raw"] != "") & (df["round"] == "")
    df["response"] = df["response_raw"].map(lambda s: re.sub(r"\s+", " ", s.strip()) if isinstance(s, str) else "")
    df["response_reported"] = df["response"]
    df["campaign_source"] = np.where(df["response"] != "", "reported", "")
    df["campaign_start"] = pd.NaT
    if lk is not None:
        L = lk.copy()
        names = {k: g.iloc[0]["OBR Name"] for k, g in L.groupby(["_ck", "_rk"])}
        ck = df["country"].map(ckey)
        rk = df["response"].map(key)
        std = pd.Series([names.get((c, r), "") for c, r in zip(ck, rk)], index=df.index)
        in_lk = std != ""
        df.loc[in_lk, "response"] = std[in_lk]
        df.loc[in_lk, "campaign_source"] = "lookup_name"
        # campaign found in the lookup: the lookup is the authority for the round number and start date
        for (c, r), idx in df[in_lk].groupby([ck[in_lk], rk[in_lk]]).groups.items():
            g = L[(L["_ck"] == c) & (L["_rk"] == r)].sort_values("_d")
            rows = df.loc[idx]
            rd = rows["report_date"].values.astype("datetime64[ns]")
            pos = np.searchsorted(g["_d"].values.astype("datetime64[ns]"), rd, side="right") - 1
            pos_c = np.clip(pos, 0, len(g) - 1)
            gap = (rd - g["_d"].values[pos_c]) / np.timedelta64(1, "D")
            use = (pos >= 0) & (gap <= 150)
            rnd = np.where(use, g["_rn"].values[pos_c], np.nan)
            rep = rows["round"].map(lambda x: int(x[3:]) if x else np.nan).values
            sel = np.where(np.isin(rep, g["_rn"].values), rep, rnd)      # a reported round that is one of the campaign's rounds wins over the date
            starts = g.set_index("_rn")["_d"].to_dict()
            df.loc[idx, "round"] = ["Rnd%d" % int(x) if x == x else "" for x in sel]
            df.loc[idx, "campaign_start"] = pd.Series([starts.get(x, pd.NaT) if x == x else pd.NaT for x in sel], index=idx)
        # campaign missing, or typed in a way that is not a campaign name (a place, "nOPV2022", ...): the lookup round of this country
        # and vaccine that started before the report date (within 150 days); a typed GPEI-style name that is not in the lookup is kept
        pattern = df["response"].str.match(r"^[A-Za-z]{3}-\d{4}-\d{2}-\d{2}")
        todo = (~in_lk) & ((df["response"] == "") | ~pattern)
        for (c, vac), idx in df[todo].groupby([df.loc[todo, "country"].map(ckey), df.loc[todo, "vaccine"]]).groups.items():
            g = L[L["_ck"] == c].sort_values("_d")
            if g.empty:
                continue
            tok = "bopv" if str(vac).lower().startswith("b") else "nopv"
            gv = g[g["Vaccines"].astype(str).str.lower().str.contains(tok, na=False)]
            g = gv if len(gv) else g
            rd = df.loc[idx, "report_date"].values.astype("datetime64[ns]")
            pos = np.searchsorted(g["_d"].values.astype("datetime64[ns]"), rd, side="right") - 1
            pos_c = np.clip(pos, 0, len(g) - 1)
            gap = (rd - g["_d"].values[pos_c]) / np.timedelta64(1, "D")
            use = (pos >= 0) & (gap <= 150)
            if not use.any():
                continue
            sel = g.iloc[pos_c[use]]
            ii = idx[use]
            df.loc[ii, "response"] = sel["OBR Name"].values
            df.loc[ii, "round"] = ["Rnd%d" % int(x) if x == x else "" for x in sel["_rn"].values]
            df.loc[ii, "campaign_start"] = sel["_d"].values
            df.loc[ii, "campaign_source"] = "lookup_date"
            flags.loc[ii, "campaign_from_lookup_date"] = True
        flags["campaign_not_in_lookup"] = (df["response"] != "") & (df["campaign_source"] == "reported")
    df["campaign_start"] = pd.to_datetime(df["campaign_start"], errors="coerce")
    df["campaign_start"] = df["campaign_start"].fillna(df["campaign_start_reported"])
    # --- stock arithmetic (the formulas of the form, the same for every country)
    z = lambda c: df[c].fillna(0)
    df["unaccounted_vials"] = df["vials_distributed"] - (df["unusable_back_from_lower"] + df["usable_back_from_lower"])
    df["doses_used"] = (df["unusable_back_from_lower"] + df["unaccounted_vials"]) * df["doses_per_vial"]
    df["wastage_rate"] = ((df["doses_used"] - df["children_vaccinated"]) / df["doses_used"] * 100).where(df["doses_used"] > 0).round(2)
    df["usable_theoretical"] = z("vials_start_stock") + z("vials_received") + z("vials_replenished") + z("usable_back_from_lower") - z("vials_distributed") - z("vials_transferred")
    df.loc[df["vials_distributed"].isna() & df["usable_physical"].isna(), "usable_theoretical"] = np.nan
    df["physical_vs_theoretical"] = df["usable_theoretical"] - df["usable_physical"]
    flags["returned_exceeds_distributed"] = df["unaccounted_vials"] < 0
    flags["returned_below_distributed"] = df["unaccounted_vials"] > 0
    flags["stock_mismatch"] = df["physical_vs_theoretical"].abs() > 0
    flags["wastage_negative"] = df["wastage_rate"] < 0
    flags["wastage_over_100"] = df["wastage_rate"] > 100
    df["qc_flags"] = flags.apply(lambda r: ";".join(c for c in flags.columns if r[c]), axis=1)
    return df, flags


def dedupe(df):
    """one form per place and round: the same place, campaign, round and level more than once -> the latest submission is kept."""
    k = (df["iso3"] + "|" + df["response"].map(key) + "|" + df["round"] + "|" + df["level"] + "|" + df["region"].map(key) + "|" +
         df["district"].map(key) + "|" + df["facility"].map(key))
    has = (df["response"] != "") & (df["round"] != "")
    geo_ok = ((df["level"] == "National") | ((df["level"] == "Regional") & (df["region"] != "")) | ((df["level"] == "District") & (df["district"] != "")) |
              ((df["level"] == "Facility") & (df["facility"] != "")))
    k = k.where(has & geo_ok, df["record_uuid"].astype(str))          # a record with no campaign or round is never merged with another
    # the same place reporting again within 75 days is the same report (an update); further apart it is another round that was given the same name
    df = df.assign(_k=k).sort_values(["_k", "report_date", "submission_time"])
    gap = df.groupby("_k")["report_date"].diff().dt.days
    df["_k"] = df["_k"] + "|" + (gap.isna() | (gap > 75)).groupby(df["_k"]).cumsum().astype(str)
    df = df.sort_values("submission_time")
    n0 = len(df)
    df["repeats_of_this_place"] = df.groupby("_k")["_k"].transform("size")
    df = df.drop_duplicates("_k", keep="last").drop(columns="_k")
    return df.sort_values(["iso3", "report_date"]).reset_index(drop=True), n0 - len(df)


# ------------------------------------------------------------------------------------------------ outputs
REPO_COLS = ["record_uuid", "iso3", "country", "form_id", "template", "vaccine", "language", "response", "response_reported", "round", "campaign_start", "campaign_source",
             "report_date", "report_year", "date_source", "submission_time", "level", "region", "district", "facility", "latitude", "longitude",
             "target_population", "doses_per_vial", "vials_start_stock", "vials_received", "vials_replenished", "vials_transferred", "vials_distributed",
             "usable_back_from_lower", "unusable_back_from_lower", "usable_returned_higher", "unusable_returned_higher", "usable_physical",
             "unusable_physical", "unusable_disposed", "children_vaccinated", "unaccounted_vials", "doses_used", "wastage_rate", "form_wastage_rate",
             "usable_theoretical", "physical_vs_theoretical", "reverse_logistics_on_time", "repeats_of_this_place", "qc_flags", "vam_comments"]


def summaries(df):
    sums = ["vials_start_stock", "vials_received", "vials_replenished", "vials_transferred", "vials_distributed", "usable_back_from_lower",
            "unusable_back_from_lower", "usable_returned_higher", "usable_physical", "unusable_physical", "unusable_disposed", "children_vaccinated",
            "unaccounted_vials", "doses_used"]
    base = df[(df["response"] != "") & (df["round"] != "")]
    def agg(g, by):
        a = g.groupby(by, dropna=False).agg(reports=("record_uuid", "size"), reports_with_flags=("qc_flags", lambda s: int((s != "").sum())),
                                           first_report=("report_date", "min"), last_report=("report_date", "max"),
                                           **{c: (c, lambda s: s.sum(min_count=1)) for c in sums}).reset_index()
        a["wastage_rate"] = ((a["doses_used"] - a["children_vaccinated"]) / a["doses_used"] * 100).where(a["doses_used"] > 0).round(2)
        for c in ("first_report", "last_report"):
            a[c] = a[c].dt.strftime("%Y-%m-%d")
        return a
    country = agg(base, ["iso3", "country", "response", "round", "level"])
    country["level"] = pd.Categorical(country["level"], LEVEL_ORDER, ordered=True)
    country = country.sort_values(["iso3", "response", "round", "level"]).reset_index(drop=True)
    district = agg(base[base["level"] == "Facility"], ["iso3", "country", "response", "round", "region", "district"])
    return country, district


def dictionary(df, forms):
    desc = {
        "record_uuid": "ONA unique id of the submission", "iso3": "Country ISO3 code", "country": "Country name",
        "form_id": "ONA form id", "template": "Version of the form: vials (usable_vials_*), daily (*_day, Campaign_Day) or bottles (bottles_*)",
        "vaccine": "Vaccine of the form (nOPV2 or bOPV), from the form title", "language": "Language of the form (en, fr, pt)",
        "response": "Campaign name: the OBR Name of the lookup (matched on the typed name, or on country + vaccine + report date when the typed name is empty or not a campaign name); a typed campaign name that is not in the lookup is kept as typed", "response_reported": "Campaign name as typed in the form", "round": "Round (Rnd1, Rnd2 ...), the lookup's round number when the campaign is in the lookup",
        "campaign_start": "Start date of the round (lookup; else the date reported in the form)", "campaign_source": "reported / lookup_name (name matched to the lookup) / lookup_date (campaign found from the report date)",
        "report_date": "Date of the report: the date typed in the form if plausible, else the submission date", "report_year": "Year of report_date",
        "date_source": "form or submission", "submission_time": "Time the submission reached ONA (UTC)",
        "level": "Level of the report: National, Regional, District, Facility (health facility / sub-district)", "region": "Region (capitals)",
        "district": "District (capitals)", "facility": "Health facility / sub-district (capitals)", "latitude": "GPS latitude of the report", "longitude": "GPS longitude of the report",
        "target_population": "Target population of the area (only some versions of the form ask for it)", "doses_per_vial": "Doses per vial (5, 10, 20, 25, 50, 100; other values removed)",
        "vials_start_stock": "Usable vials in stock at the start of the round", "vials_received": "Usable vials received for the round", "vials_replenished": "Usable vials resupplied during the round",
        "vials_transferred": "Usable vials transferred to another place during the round", "vials_distributed": "Usable vials distributed from this place",
        "usable_back_from_lower": "Usable vials received back from the lower level", "unusable_back_from_lower": "Unusable vials received back from the lower level",
        "usable_returned_higher": "Usable vials returned to the higher level", "unusable_returned_higher": "Unusable vials returned to the higher level (only the oldest version asks)",
        "usable_physical": "Usable vials counted on site at the end", "unusable_physical": "Unusable vials counted on site at the end", "unusable_disposed": "Unusable vials disposed on site",
        "children_vaccinated": "Children vaccinated (reported by this place)", "unaccounted_vials": "vials_distributed - (usable + unusable vials back from the lower level). Negative = more vials back than distributed",
        "doses_used": "(unusable_back_from_lower + unaccounted_vials) x doses_per_vial: doses taken out of the vials that were not returned usable",
        "wastage_rate": "(doses_used - children_vaccinated) / doses_used x 100, recomputed the same way for every country", "form_wastage_rate": "Wastage rate the form itself calculated (for comparison)",
        "usable_theoretical": "vials_start_stock + vials_received + vials_replenished + usable_back_from_lower - vials_distributed - vials_transferred",
        "physical_vs_theoretical": "usable_theoretical - usable_physical (0 = the stock matches)", "reverse_logistics_on_time": "Were the reports and vials sent to the upper level on time (Yes / No / NA)",
        "repeats_of_this_place": "How many submissions the same place, campaign, round and level had within 75 days (the latest is kept)", "qc_flags": "Semicolon list of checks that failed", "vam_comments": "Free-text comment of the vaccine accountability monitor",
    }
    flags = {"implausible_number": "A figure is negative or above 5,000,000: set to empty", "doses_per_vial_odd": "doses_per_vial not 1, 2, 5, 10, 20, 25, 50 or 100: set to empty (doses used and wastage are then empty)",
             "round_not_a_round": "The round question holds something that is not a round number (a campaign name, a date)", "date_from_submission": "The date typed in the form is missing or implausible: submission date used",
             "level_inferred": "The level of the report was empty: guessed from the geography filled", "no_geography": "The level needs a name (region, district, facility) that is empty", "geography_below_level": "Names below the level are filled (a National or District report that also names a district or facility): kept as reported",
             "returned_exceeds_distributed": "More vials came back from the lower level than this place distributed", "returned_below_distributed": "Fewer vials came back than distributed (unaccounted vials)",
             "stock_mismatch": "Theoretical usable stock differs from the physical count", "wastage_negative": "Children vaccinated exceed the doses used", "wastage_over_100": "Wastage rate above 100%",
             "no_figures": "No vial or children figure at all", "campaign_not_in_lookup": "A typed campaign name (country-year-... style) that is not in the lookup: kept as typed", "campaign_from_lookup_date": "The typed campaign was empty or not a campaign name: found in the lookup from country, vaccine and report date"}
    cols = pd.DataFrame([{"Column": c, "Description": desc.get(c, "")} for c in REPO_COLS])
    fl = pd.DataFrame([{"Flag": k, "Meaning": v} for k, v in flags.items()])
    fm = forms[["form_id", "country", "iso3", "name", "variant", "language", "enabled", "submissions_on_ona", "last_submission", "note"]].copy()
    rules = pd.DataFrame({"Rule": [
        "One record = one report of one place (national, regional, district or facility) for one round.",
        "Repeated submissions of the same place, campaign, round and level within 75 days of each other: the latest is kept (repeats_of_this_place says how many). Reports of the same place more than 75 days apart are kept as separate reports (another round under the same name).",
        "Reports of different levels are never added together in the repository. afro_form_a_country_round.csv groups by level; afro_form_a_district_round.csv adds only facility-level reports.",
        "Questions of the three versions of the form (vials, daily, bottles) are mapped to the same column (see process_form_a.py, FIGURES).",
        "Place names: capitals, underscores and extra spaces removed. No other correction of names is made.",
        "The campaign name and round follow the lookup (GPEI), as in the admin data: Response = OBR Name, round = the lookup's Rnd number.",
        "Names and phone numbers of people (officers, monitors) are not kept.",
        "The wastage rate and the stock arithmetic are recomputed with one formula for all countries; the form's own wastage rate is kept as form_wastage_rate."]})
    with pd.ExcelWriter(META / "AFRO_form_a_dictionary.xlsx", engine="openpyxl") as xw:
        cols.to_excel(xw, sheet_name="Columns", index=False)
        fl.to_excel(xw, sheet_name="QC_flags", index=False)
        fm.to_excel(xw, sheet_name="Forms", index=False)
        rules.to_excel(xw, sheet_name="Rules", index=False)
        pd.DataFrame({"Item": ["Author", "Built", "Records"], "Value": ["Gorgui Ba Toure", datetime.now().strftime("%Y-%m-%d"), len(df)]}).to_excel(xw, sheet_name="About", index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(100, max(12, max(len(str(c.value or "")) for c in col[:60]) + 2))


def main():
    forms = pd.read_csv(HERE / "config" / "forms.csv", encoding="utf-8-sig", dtype=str).fillna("")
    use = forms[forms["enabled"].str.upper().isin(["TRUE", "T", "1", "YES"])]
    lk = read_lookup()
    parts = []
    for _, r in use.iterrows():
        d = load_form(r)
        if d is None:
            log(f"no data file for {r['iso3']}_{r['form_id']}: run fetch_ona_data.py")
            continue
        parts.append(d)
    if not parts:
        sys.exit("no raw data: run fetch_ona_data.py first")
    df = pd.concat(parts, ignore_index=True)
    n_raw = len(df)
    log(f"{n_raw:,} submissions from {len(parts)} forms")
    df, flags = clean(df, lk)
    df, n_rep = dedupe(df)
    log(f"{n_rep:,} repeated submissions removed -> {len(df):,} reports")
    for c in REPO_COLS:
        if c not in df.columns:
            df[c] = ""
    out = df[REPO_COLS].copy()
    for c in ("campaign_start", "report_date"):
        out[c] = pd.to_datetime(out[c], errors="coerce").dt.strftime("%Y-%m-%d")
    out["submission_time"] = pd.to_datetime(out["submission_time"], errors="coerce", utc=True).dt.strftime("%Y-%m-%d %H:%M:%S")
    FINAL.mkdir(parents=True, exist_ok=True)
    META.mkdir(parents=True, exist_ok=True)
    REP.mkdir(exist_ok=True)
    out.to_csv(FINAL / "afro_form_a_repository.csv", index=False, encoding="utf-8-sig")
    country, district = summaries(df)
    country.to_csv(FINAL / "afro_form_a_country_round.csv", index=False, encoding="utf-8-sig")
    district.to_csv(FINAL / "afro_form_a_district_round.csv", index=False, encoding="utf-8-sig")
    dictionary(df, forms)
    # quality summary
    fl = df["qc_flags"].str.split(";").explode()
    fl = fl[fl != ""].value_counts()
    unmatched = df[df["qc_flags"].str.contains("campaign_not_in_lookup")].groupby(["iso3", "response"]).size().rename("reports").reset_index()
    unmatched.to_csv(REP / "form_a_unmatched_campaigns.csv", index=False, encoding="utf-8-sig")
    qc = {"built": datetime.now().isoformat(timespec="seconds"), "submissions_read": int(n_raw), "repeats_removed": int(n_rep), "reports": int(len(df)),
          "by_level": df["level"].value_counts().to_dict(), "by_template": df["template"].value_counts().to_dict(), "flags": {k: int(v) for k, v in fl.items()},
          "reports_without_campaign": int((df["response"] == "").sum()), "reports_without_round": int((df["round"] == "").sum()),
          "countries": int(df["iso3"].nunique())}
    (REP / "qc_summary.json").write_text(json.dumps(qc, indent=2), encoding="utf-8")
    log(json.dumps(qc))
    log(f"written: data/final/afro_form_a_repository.csv ({len(out):,} rows), country_round ({len(country):,}), district_round ({len(district):,}), dictionary")


if __name__ == "__main__":
    main()
