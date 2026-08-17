# Detailed Methodology

This document explains, step by step, the techniques used in each stage of the two modules. The idea is that anyone — even without reading the code — can understand **what** each part does and **why**.

---

## 1. EEVEE — historical learning

### 1.1 Unification of rainfall data
Each rainfall station exports its own spreadsheet, with different column names and formats. The script automatically identifies the date and rainfall columns (looking for words like "data", "chuva", "mm"), converts everything to a common hourly base, and builds a single table (rows = hours, columns = stations).

### 1.2 Rainfall spatialization (which method "estimates best"?)
Not every point in the basin has a rain gauge. Therefore, 4 classic ways to estimate rainfall at a point from neighboring stations were tested:

| Method | Core idea |
|---|---|
| **Simple Mean** | Arithmetic mean of neighboring stations |
| **Thiessen (polygons)** | Uses the value of the nearest station |
| **IDW** (Inverse Distance Weighting) | Weights by the inverse of the cubed distance — closer stations weigh more |
| **Spline (Rbf)** | Smooth interpolation using radial basis functions |

To choose the best one, the script performs a **"leave-one-out" cross-validation** test: for each station, it pretends it doesn't exist, estimates the rainfall there using the others, and compares it with the real value. The method with the lowest **RMSE** (root mean square error) — meaning the one closest to reality — is automatically chosen.

### 1.3 Design storm scenario + HAND correction
The script identifies the **most critical day** ever recorded (highest volume of simultaneous rainfall across several stations) to serve as a reference scenario, and corrects the **HAND** (Height Above the Nearest Drainage) raster by removing spurious negative values that would otherwise disrupt the risk map later.

### 1.4 Hydraulic model optimization (Muskingum-Cunge)
**Muskingum-Cunge** is a classic flood wave routing model in rivers: it describes how a peak flow entering the headwaters of the river attenuates and delays until it reaches the outlet (the basin's exit point). This model depends on physical parameters of the river (width, slope, roughness) that are rarely known with precision.

The script uses **differential evolution optimization** (`scipy.optimize.differential_evolution`) to automatically adjust these parameters, minimizing the error between the simulated flow and the actual measured flow at the outlet station, for the available historical events.

### 1.5 AI Training (Random Forest)
For each time step, the script creates a "snapshot" with variables such as rainfall in the last hour, simulated flow, 24h accumulated rainfall, and terrain characteristics (HAND). These snapshots are labeled into 3 risk classes:

- **0** — dry weather / safe condition
- **1** — previously recorded historical flood
- **2** — severe flood (high return period synthetic scenario)

A **Random Forest** (an ensemble of decision trees) learns to associate the condition "snapshot" with the risk class. The model's quality is reported by the **OOB score** (estimated accuracy without needing a separate test set) and by the **feature importance** of each variable in the decision — which helps interpret *why* the AI is predicting high risk.

### 1.6 Reports
Two interactive HTML reports (Plotly) are generated: hydrograph vs. hyetograph (rainfall) of the 5 largest events, and the wave routing at three points in the river (source, middle, outlet).

---

## 2. JOLTEON — operational nowcasting

### 2.1 SCS-CN Hydrograph
From the observed rainfall, the script calculates the **effective rainfall** (the portion that actually becomes surface runoff, discounting infiltration) using the **SCS-CN** (Soil Conservation Service — Curve Number) method, a standard hydrology method that depends on a parameter (**CN**) related to soil type and urban basin usage. The resulting hydrograph is compared with a **synthetic reference storm**, built using the **IDF (Intensity-Duration-Frequency) curve for Curitiba (created by Fendrich, used for code development)** — this provides a benchmark: "is this actual rainfall close to an event with a Return Period of how many years?"

### 2.2 Muskingum-Cunge routing by cross-sections
Using the parameters adjusted by EEVEE, the flood wave is routed section by section along the river, allowing the estimation of the peak flow and arrival time at each reach — not just at the final outlet.

### 2.3 Spatial risk classification
Each raster cell (map pixel) receives a classification combining:
- **HAND** (height above drainage — physical proximity to overflow risk);
- Pre-calculated **flood threshold**;
- **Random Forest probability** trained by EEVEE, applied to current rainfall and flow conditions.

The result classifies each point as: safe, pluvial flood (lack of drainage), fluvial flood (river overflow), or mixed collapse (both effects together).

### 2.4 Interactive map (WebGIS)
A Folium map gathers: basin boundary, drainage network, Muskingum-Cunge control sections, and the classified risk points — navigable, with layers that can be toggled on/off, using Google Earth Engine as the base map.

### 2.5 Decision dashboard
For each river section, the script calculates:
- the estimated time until peak flow (with sub-hourly parabolic interpolation, for greater precision than the raw time step);
- whether the predicted flow exceeds the channel's capacity in that section;
- the recommended action: 🟢 safe margin, 🟡 monitoring, 🔴 red alert, or 🚨 evacuation with estimated time in minutes.

---

## 3. Quick glossary

| Term | Meaning |
|---|---|
| **HAND** | Height Above the Nearest Drainage — height of the terrain above the nearest watercourse; the lower it is, the greater the flood risk |
| **RMSE** | Root Mean Square Error — measures how far an estimate is from the actual value |
| **IDW** | Inverse Distance Weighting — spatial interpolation weighted by distance |
| **Muskingum-Cunge** | Hydraulic model that simulates the routing of a flood wave along a river |
| **SCS-CN** | Standard method to estimate effective rainfall (runoff) from total rainfall and soil type/land use |
| **IDF** | Intensity-Duration-Frequency curve — relates rainfall intensity, duration, and return period |
| **TR (Tempo de Retorno)** | Return Period — average time, in years, between occurrences of rainfall events of a given magnitude |
| **Random Forest** | Machine learning algorithm that combines multiple decision trees to classify or predict |
| **OOB score** | Out-of-bag score — accuracy estimate of a Random Forest using data unseen by each individual tree |
| **Exutório** | Basin outlet — the exit point of a watershed, where all drained water converges |
