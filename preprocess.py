"""
preprocess.py — Run this ONCE before launching the Streamlit app.
This processes the raw CSV and creates a cache.pkl file for fast loading.

Usage:
    python preprocess.py --input <path_to_csv>

Example:
    python preprocess.py --input jan_to_may_police_violation_anonymized791b166.csv
"""

import argparse
import pandas as pd
import numpy as np
import h3
import json
import pickle
import datetime
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.preprocessing import LabelEncoder

def run(input_csv):
    print(f"[1/6] Loading data from: {input_csv}")
    df = pd.read_csv(input_csv)
    df['created_datetime'] = pd.to_datetime(df['created_datetime'], format='mixed', utc=True)
    df = df.dropna(subset=['latitude', 'longitude'])
    print(f"      Loaded {len(df):,} records")

    print("[2/6] Extracting time features...")
    df['hour'] = df['created_datetime'].dt.hour
    df['dayofweek'] = df['created_datetime'].dt.dayofweek
    df['date'] = df['created_datetime'].dt.date
    df['month'] = df['created_datetime'].dt.month

    df['is_parking_main_road'] = df['violation_type'].str.contains('MAIN ROAD', na=False)
    df['is_wrong_parking'] = df['violation_type'].str.contains('WRONG PARKING', na=False)
    df['is_no_parking'] = df['violation_type'].str.contains('NO PARKING', na=False)

    print("[3/6] Computing H3 hexagonal grid (resolution 8)...")
    df['h3_cell'] = df.apply(lambda r: h3.latlng_to_cell(r['latitude'], r['longitude'], 8), axis=1)

    print("[4/6] Computing Parking Impact Scores...")
    hex_agg = df.groupby('h3_cell').agg(
        violation_count=('id', 'count'),
        lat=('latitude', 'mean'),
        lon=('longitude', 'mean'),
        main_road_pct=('is_parking_main_road', 'mean'),
        wrong_parking_pct=('is_wrong_parking', 'mean'),
        no_parking_pct=('is_no_parking', 'mean'),
        unique_stations=('police_station', 'nunique'),
    ).reset_index()

    hex_agg['main_road_weight'] = hex_agg['main_road_pct'] * 0.5
    hex_agg['density_factor'] = np.log1p(hex_agg['violation_count']) / np.log1p(hex_agg['violation_count'].max())
    hex_agg['PIS'] = (hex_agg['violation_count'] * (1 + hex_agg['main_road_weight']) * (1 + hex_agg['density_factor'])).round(1)
    hex_agg['PIS_norm'] = (hex_agg['PIS'] / hex_agg['PIS'].max() * 100).round(1)

    top_hex = hex_agg.nlargest(200, 'PIS').reset_index(drop=True)
    top_hex['rank'] = range(1, len(top_hex) + 1)
    top20_cells = top_hex.head(20)['h3_cell'].tolist()

    print("[5/6] Training ML forecast model...")
    ml_data = df.groupby(['h3_cell', 'hour', 'dayofweek']).size().reset_index(name='count')
    ml_data = ml_data[ml_data['h3_cell'].isin(top20_cells)]

    le = LabelEncoder()
    ml_data['cell_enc'] = le.fit_transform(ml_data['h3_cell'])

    X = ml_data[['cell_enc', 'hour', 'dayofweek']].values
    y = ml_data['count'].values

    model = GradientBoostingRegressor(n_estimators=100, max_depth=4, random_state=42)
    model.fit(X, y)

    today_dow = datetime.datetime.now().weekday()
    forecast_rows = []
    for cell in top_hex.head(10)['h3_cell'].tolist():
        if cell in le.classes_:
            cell_enc = le.transform([cell])[0]
            for h_val in range(24):
                pred = max(0, model.predict([[cell_enc, h_val, today_dow]])[0])
                forecast_rows.append({'h3_cell': cell, 'hour': h_val, 'predicted_violations': round(pred, 1)})
    forecast_df = pd.DataFrame(forecast_rows)

    print("[6/6] Saving cache.pkl...")
    cache = {
        'hex_agg': hex_agg,
        'top_hex': top_hex,
        'daily': df.groupby('date').size().reset_index(name='count').assign(date=lambda x: pd.to_datetime(x['date'])),
        'vehicle_dist': df['vehicle_type'].value_counts().head(8).reset_index().rename(columns={'vehicle_type': 'vehicle_type', 'count': 'count'}),
        'hourly_total': df.groupby('hour').size().reset_index(name='count'),
        'station_summary': df.groupby('police_station').agg(
            violations=('id', 'count'),
            area_lat=('latitude', 'mean'),
            area_lon=('longitude', 'mean'),
        ).reset_index().nlargest(20, 'violations'),
        'viol_types': {
            'Wrong Parking': int(df['is_wrong_parking'].sum()),
            'No Parking': int(df['is_no_parking'].sum()),
            'Parking on Main Road': int(df['is_parking_main_road'].sum()),
            'Other': int((~df['is_wrong_parking'] & ~df['is_no_parking'] & ~df['is_parking_main_road']).sum()),
        },
        'hourly_top20': df[df['h3_cell'].isin(top20_cells)].groupby(['h3_cell', 'hour']).size().reset_index(name='count'),
        'forecast_df': forecast_df,
        'top20_cells': top20_cells,
        'total_records': len(df),
        'date_range': (str(df['created_datetime'].min().date()), str(df['created_datetime'].max().date())),
    }

    with open('cache.pkl', 'wb') as f:
        pickle.dump(cache, f)

    print(f"\n✅ Done! cache.pkl saved.")
    print(f"   Records: {len(df):,}")
    print(f"   Hex zones: {len(hex_agg):,}")
    print(f"   Top PIS: {top_hex['PIS_norm'].max():.1f}/100")
    print(f"\nNow run:  streamlit run app.py")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True, help='Path to violation CSV')
    args = parser.parse_args()
    run(args.input)
