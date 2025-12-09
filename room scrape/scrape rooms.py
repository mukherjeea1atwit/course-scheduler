import pandas as pd

# -------- CONFIG --------
excel_file = "SCDS course schedul data (2022-2024).xlsx"  # change path if needed
output_csv = "classroom_capacities.csv"

# Load all sheet names
xls = pd.ExcelFile(excel_file)
frames = []

for sheet_name in xls.sheet_names:
    # Read the sheet once
    df = pd.read_excel(excel_file, sheet_name=sheet_name)
    # Strip whitespace off column names
    df.columns = [str(c).strip() for c in df.columns]

    # Check if we already have Location + Cap(/Cap.)
    if "Location" not in df.columns or ("Cap." not in df.columns and "Cap" not in df.columns):
        # Try to detect a header row inside the data (e.g., SPRING 2022)
        df_raw = pd.read_excel(excel_file, sheet_name=sheet_name, header=None)

        header_row_idx = None
        for i in range(len(df_raw)):
            row = df_raw.iloc[i].astype(str).str.strip()
            if "Location" in row.values and ("Cap." in row.values or "Cap" in row.values):
                header_row_idx = i
                break

        # If we can't find a header row, skip this sheet (e.g., random Sheet1)
        if header_row_idx is None:
            print(f"Skipping sheet '{sheet_name}' (no Location/Cap header found)")
            continue

        # Build a proper DataFrame using the detected header row
        header = df_raw.iloc[header_row_idx].astype(str).str.strip()
        data = df_raw.iloc[header_row_idx + 1 :].reset_index(drop=True)
        data.columns = header
        df = data
        df.columns = [str(c).strip() for c in df.columns]

    # Decide which capacity column to use
    cap_col = None
    if "Cap." in df.columns:
        cap_col = "Cap."
    elif "Cap" in df.columns:
        cap_col = "Cap"

    # If still missing, skip this sheet
    if cap_col is None or "Location" not in df.columns:
        print(f"Skipping sheet '{sheet_name}' (missing Location or Cap column)")
        continue

    # Extract only Location and Cap
    temp = df[["Location", cap_col]].copy()

    # Drop rows with no classroom name
    temp = temp.dropna(subset=["Location"])

    # Convert capacity to numeric
    temp[cap_col] = pd.to_numeric(temp[cap_col], errors="coerce")
    temp = temp.dropna(subset=[cap_col])
    temp[cap_col] = temp[cap_col].astype(int)

    # Normalize column name to 'Cap'
    temp.rename(columns={cap_col: "Cap"}, inplace=True)

    frames.append(temp)

# Combine results from all sheets
if not frames:
    raise ValueError("No sheets with usable 'Location' and 'Cap' data were found.")

all_rooms = pd.concat(frames, ignore_index=True)

# Get unique classrooms with their maximum capacity across all terms
room_caps = all_rooms.groupby("Location", as_index=False)["Cap"].max()

# Save to CSV
room_caps.to_csv(output_csv, index=False)

print(f"✅ Saved classroom capacities to: {output_csv}")
