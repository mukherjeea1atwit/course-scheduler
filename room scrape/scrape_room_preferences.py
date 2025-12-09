import pandas as pd
import numpy as np
import re

def build_room_preferences(input_path: str) -> pd.DataFrame:
    # Read with no header because the file repeats header rows
    raw = pd.read_excel(input_path, header=None)

    # Detect header row (first row whose first cell is "CRN" after stripping)
    col0_stripped = raw.iloc[:, 0].astype(str).str.strip()
    header_mask = col0_stripped.eq("CRN")
    if not header_mask.any():
        raise ValueError("Could not find header row with 'CRN' in first column")

    header_row = raw[header_mask].iloc[0].astype(str).str.strip()
    cols = header_row.tolist()

    # Drop header rows and completely empty rows
    df = raw[~header_mask].copy()
    df = df.dropna(how="all")

    # Assign column names (truncate extras)
    df.columns = cols + [f"extra_{i}" for i in range(len(df.columns) - len(cols))]
    df = df[cols]

    # Helper to clean strings
    def clean(s):
        if pd.isna(s):
            return np.nan
        return re.sub(r"\s+", " ", str(s).strip())

    # Clean key string columns
    for col in ["Subj", "Crse", "Section", "Location", "Title", "Days", "Time", "Instructor"]:
        if col in df.columns:
            df[col] = df[col].map(clean)

    # Numeric columns
    for col in ["Credit", "Cap.", "Act.", "Rem"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Course ID
    df["Course"] = df["Subj"] + " " + df["Crse"]

    # Lecture vs Lab
    is_lab = df["Title"].str.contains("LAB", case=False, na=False) | (df["Credit"].fillna(0) == 0)
    df["Type"] = np.where(is_lab, "Lab", "Lecture")

    # Aggregate by Course + Type + Location
    agg = (
        df.groupby(["Course", "Type", "Location"], dropna=True)
          .agg(
              times_offered=("CRN", "nunique"),
              max_cap=("Cap.", "max"),
          )
          .reset_index()
    )

    # Sort & rank preferences
    agg = agg.sort_values(
        ["Course", "Type", "times_offered", "max_cap"],
        ascending=[True, True, False, False],
    )
    agg["PreferenceRank"] = agg.groupby(["Course", "Type"]).cumcount() + 1

    # Only keep requested columns
    out = agg[["Course", "Type", "PreferenceRank", "Location", "max_cap"]]

    return out


if __name__ == "__main__":
    input_file = "/Users/aakashmkj/WIT Class Scheduler/room scrape/SCDS course schedul data (2022-2024).xlsx"
    output_file = "room_preferences.csv"

    prefs_df = build_room_preferences(input_file)
    prefs_df.to_csv(output_file, index=False)

    print(f"Room preference CSV written to: {output_file}")
