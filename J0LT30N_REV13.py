# ==============================================================================
# ⚡ JOLTEON: TACTICAL FLOOD NOWCASTING SYSTEM (MODO 3 RASTERS: MDE, HAND, LIMIAR) ⚡
# Joint Observational Learning Tool for Extreme Overland Nowcasting
# ==============================================================================
"""
JOLTEON — Sistema Tático de Nowcasting de Cheias Urbanas
================================================================

O QUE ESSE SCRIPT FAZ (em ordem):
  1. Carrega o modelo Random Forest e a calibração hidráulica gerados
     pelo EEVEE, além dos rasters de terreno (MDE, HAND, limiar de
     inundação) e camadas geoespaciais da bacia (bairros, rede de rios).
  2. Lê a chuva observada em tempo (quase) real e gera o hidrograma da
     bacia pelo método SCS-CN, comparando com uma tempestade sintética
     de referência (equação IDF de Curitiba) para dar contexto de
     severidade (ex: "essa chuva equivale a um TR de X anos").
  3. Propaga a onda de cheia gerada seção por seção do rio, usando o
     modelo Muskingum-Cunge calibrado pelo EEVEE.
  4. Classifica o risco espacial de alagamento (pluvial / fluvial /
     misto) pixel a pixel, cruzando o terreno (MDE/HAND) com a
     probabilidade prevista pela Random Forest.
  5. Gera um mapa interativo (Folium/WebGIS) com bairros, rede de
     drenagem, seções de controle e pontos de risco classificados.
  6. Imprime um painel tático: veredito de risco geral, gargalo
     hidráulico crítico do rio, tempo estimado até o pico de vazão em
     cada seção (com interpolação sub-horária) e a ação recomendada
     (evacuar / alerta / monitorar / margem segura).

ENTRADAS ESPERADAS: ver dados/README.md (inclui o modelo .pkl gerado
                     pelo eevee_rev_7.py)
SAÍDAS GERADAS: mapa_webgis.html, gráficos de hidrograma (Plotly/Matplotlib),
                malha_inundacao_JOLTEON.shp (em dados/saida_dados/)

⚠️ Rode o eevee_rev_7.py primeiro — este script depende do modelo e da
   calibração que ele gera.
"""
import pandas as pd
import numpy as np
import geopandas as gpd
import os
import sys
import time
import math
import random
import joblib
import warnings
import rasterio
from datetime import datetime
from openpyxl import load_workbook
from deap import base, creator, tools
from shapely.ops import linemerge
from shapely.geometry import Point, Polygon, MultiPolygon
import matplotlib.pyplot as plt
import matplotlib.colors as colors
from scipy.spatial import cKDTree
from sklearn.ensemble import RandomForestClassifier
from tqdm import tqdm
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import webbrowser
import platform
import subprocess
from scipy.interpolate import interp1d

# Tentativa de importação do motor WebGIS
try:
    import folium
    from folium import plugins
    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False

warnings.filterwarnings('ignore')

# ==========================================================
# 0. ESTÉTICA E PROTOCOLOS DE COMUNICAÇÃO 
# ==========================================================
def log_inicio(msg): 
    print(f"\n\033[1;37m[SISTEMA]\033[0m \033[94mIniciando Protocolo:\033[0m {msg}...")

def log_sucesso(msg): 
    print(f"\033[1;32m[ESTÁVEL]\033[0m Sincronização: {msg} ✅")

def log_alerta(msg): 
    print(f"\033[1;31m[ALERTA CRÍTICO]\033[0m \033[1;93m{msg}\033[0m ⚡")

def abrir_html(caminho):
    try:
        if platform.system() == 'Darwin': 
            subprocess.call(['open', caminho])
        elif platform.system() == 'Windows':
            os.startfile(caminho)
        else:
            webbrowser.open('file://' + os.path.realpath(caminho))
    except:
        pass

def jolteon_startup():
    print("\n" + "█"*90)
    print("  JOLTEON | [J]oint [O]bservational [T]ool for [E]xtreme [O]verland [N]owcasting [S]ystem  ".center(90))
    print("  MONITORAMENTO TÁTICO E ANÁLISE PROBABILÍSTICA (MDE + HAND + LIMIAR SEPARADOS) ".center(90))
    print("█"*90)
    print(f"  [STATUS] Núcleo de Processamento: ATIVO")
    print(f"  [STATUS] Motor WebGIS: {'Ativado (Folium)' if HAS_FOLIUM else 'Desativado (Falta Biblioteca)'}")
    print(f"  [STATUS] Registro Temporal: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("█"*90)

jolteon_startup()

# ==========================================================
# 1. CARREGAMENTO DE DADOS E CAMINHOS
# ==========================================================
log_inicio("Acessando repositório de dados hidrométricos e espaciais 📂")

# ⚙️ Edite os caminhos abaixo para apontar para as suas pastas locais de dados
# (por padrão, tudo aponta para a pasta "dados/" na raiz do repositório)
BASE_DADOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados")
ENTRADA = os.path.join(BASE_DADOS, "entrada_jolteon")

# Dados de entrada do hidrograma
hidro = pd.read_excel(os.path.join(ENTRADA, "dados_hidrograma.xlsx"), index_col=0)
area_bacia = hidro.loc["area_bacia", "valor"]               
comp_rio = hidro.loc["comp_rio", "valor"]                   
desnivel = hidro.loc["desnivel", "valor"]                   
cn = hidro.loc["cn", "valor"]                               
nome_bacia = hidro.loc["nome_bacia", "valor"]               
tempo_inicial = hidro.loc["tempo_inicial", "valor"]         
n_pontosin = hidro.loc["n_pontos", "valor"]                 
planilha_precipitacao = os.path.join(ENTRADA, "precipitacao_tempo_real.xlsx")

# Dados de entrada do Muskingum-Cunge
muskingum = pd.read_excel(os.path.join(ENTRADA, "dados_muskingum.xlsx"), index_col=0)
distancia_cidades = muskingum.loc["distancia_cidades", "valor"]  
b_o = muskingum.loc["b_o", "valor"]                              
S_o = muskingum.loc["S_o", "valor"]                              
n_manning = muskingum.loc["n", "valor"]                          
comprimento_rio_m = distancia_cidades * 1000                     

# Topografia vinda do QGIS (Shapefiles)
shp_bairros = os.path.join(ENTRADA, "SHP", "BAIRROS_BACIA_BELEM", "BAIRROS_BACIA_A.shp")
shp_rios    = os.path.join(ENTRADA, "SHP", "HIDROGRAFIA_RIO", "HIDROGRAFIA_BANCO.shp")

# Caminhos dos rasters (MDE, HAND e LIMIAR)
mde_tiff    = os.path.join(ENTRADA, "RF", "mdt", "mde_lidar_final5x5.tif")
# ⚠️ Verifique se os nomes abaixo estão corretos na sua pasta:
hand_tiff   = os.path.join(ENTRADA, "RF", "HAND", "output_HAND.tif")
limiar_tiff = os.path.join(ENTRADA, "RF", "HAND", "limiar_hand.tif")

# Caminhos do IA e Saídas
pasta_eevee = os.path.join(BASE_DADOS, "saida_eevee")
caminho_modelo_rf = os.path.join(pasta_eevee, "modelo_rf_jolteon.pkl")
caminho_colunas_rf = os.path.join(pasta_eevee, "colunas_rf.pkl")
pasta_saida = os.path.join(BASE_DADOS, "saida_dados")

log_sucesso("Carga de dados concluída. Sistema JOLTEON pronto para inferência.")

# ==============================================================================
# 2. HIDROGRAMA SCS-CN E PRECIPITAÇÃO EFETIVA (REAL VS SINTÉTICO)
# ==============================================================================
log_inicio("Iniciando formulação do Hidrograma SCS-CN (Comparativo) 🌧️")

# Tempo de retorno de referencia
TR_REFERENCIA = 50 # O JOLTEON criará uma chuva com TR de xx Anos para comparação visual

s = (25400/float(cn)) - 254
ia = s/5
delta_s = float(desnivel) / float(comp_rio)

# 2.1 Gerador estocástico (tempestade sntética de referencia)
log_sucesso(f"Oráculo Ativo: Gerando Tempestade Sintética (TR {TR_REFERENCIA} Anos) para calibração visual.")
passos_sint = int((24 * 60) / 60) # 24 horas (passos de 60 min)
tempos_sint = np.arange(60, (passos_sint * 60) + 1, 60)

# Equação IDF Curitiba (Fendrich)
intensidades = (5726.64 * (TR_REFERENCIA ** 0.159)) / ((tempos_sint + 41.0) ** 1.041)
p_acum_sint = intensidades * (tempos_sint / 60)

p_inc_sint = np.zeros(passos_sint)
p_inc_sint[0] = p_acum_sint[0]
for i in range(1, passos_sint): p_inc_sint[i] = p_acum_sint[i] - p_acum_sint[i-1]
    
p_inc_sint = np.sort(p_inc_sint)[::-1]
blocos_sint = np.zeros(passos_sint)
meio_sint = passos_sint // 2
for i in range(passos_sint):
    pos = meio_sint + (i // 2) if i % 2 == 0 else meio_sint - (i // 2) - 1
    if 0 <= pos < passos_sint: blocos_sint[pos] = p_inc_sint[i]
        
df_precipitacao_sint = pd.DataFrame({'tempo (min)': tempos_sint, 'p (mm)': blocos_sint})

# 2.2  Leitura da chuva (planilha excel, deve ser modificado posteriormente com a telemetria ANA)
log_sucesso("Modo Operacional Ativo: Lendo chuva em tempo real da bacia.")
df_precipitacao = pd.read_excel(planilha_precipitacao)

# 2.3 Motor SCS_CN e HU
def calcular_hidrograma_completo(df_chuva):
    df_p = df_chuva.copy()
    df_p['p acum (mm)'] = df_p['p (mm)'].cumsum()

    def calcular_pe(row):
        p_acum = row['p acum (mm)']
        if p_acum <= ia: return 0
        else: return ((p_acum - ia) ** 2) / (p_acum - ia + s)

    df_p['pe acum (mm)'] = df_p.apply(calcular_pe, axis=1)
    df_p['pe (mm)'] = df_p['pe acum (mm)'].diff().fillna(df_p['pe acum (mm)'].iloc[0])
    df_p['p inf (mm)'] = df_p['p (mm)'] - df_p['pe (mm)']

    ultima_linha_p = df_p['p (mm)'].iloc[-1]
    ultimo_valor_pacum = df_p['p acum (mm)'].iloc[-2] if ultima_linha_p == 0 else df_p['p acum (mm)'].iloc[-1]
    
    tempo_match = df_p.loc[df_p['p acum (mm)'] == ultimo_valor_pacum, 'tempo (min)']
    tempo_pacum = tempo_match.iloc[0] if not tempo_match.empty else df_p['tempo (min)'].iloc[-1]

    i_m = float(ultimo_valor_pacum) / float(tempo_pacum)
    p_t = float(i_m) * float(tempo_pacum) / 60
    t_c = 57 * (((float(comp_rio) / 1000) ** 3) / float(desnivel)) ** 0.385
    tp = 0.6 * float(t_c)
    tr = 5
    t_p = (float(tr) / 2) + (0.6 * float(t_c))
    te = 1.67 * float(t_p)
    tb = float(t_p) + float(te)
    qp = (0.208 * float(area_bacia)) / float(t_p) * 60

    pontos_hut = [(0,0), (t_p, qp), (tb, 0)]
    def equacao_reta_hut(p1, p2):
       x1, y1 = p1; x2, y2 = p2
       m = (y2 - y1) / (x2 - x1) if x2 != x1 else 0; b = y1 - m * x1
       return m, b

    reta_1 = equacao_reta_hut(pontos_hut[0], pontos_hut[1])
    reta_2 = equacao_reta_hut(pontos_hut[1], pontos_hut[2])

    n_pontos = int(n_pontosin)
    tempo_min = [tempo_inicial * i for i in range(1, n_pontos + 1)]
    qp_max = float(qp)
    qp_calculados, tempo_validos = [], []
    usar_reta_desc = False

    for t in tempo_min:
        if not usar_reta_desc:
            qp_calculado = reta_1[0] * t + reta_1[1]
            if qp_calculado >= qp_max:
                qp_calculado = qp_max; usar_reta_desc = True
        else:
            qp_calculado = reta_2[0] * t + reta_2[1]
            if qp_calculado < 0: qp_calculado = 0
        qp_calculados.append(qp_calculado)
        tempo_validos.append(t)

    df_h = pd.DataFrame({'tempo (min)': tempo_validos[:n_pontos], 'qp (m3/s.mm)': qp_calculados[:n_pontos]})
    df_pe = df_p['pe (mm)'].to_frame().T
    df_pe.columns = [f'pe{i + 1} (mm)' for i in range(len(df_p))]

    for col in df_pe.columns: df_h[col] = df_pe[col]
    for i, col in enumerate(df_pe.columns):
        valor_pe = df_pe.iloc[0, i]
        df_h[col] = 0.0
        if i > 0: df_h.loc[i:, col] = (df_h['qp (m3/s.mm)'].iloc[:-i] * valor_pe).values
        else: df_h[col] = df_h['qp (m3/s.mm)'] * valor_pe

    df_h['Q (m3/s)'] = df_h.filter(regex='^pe').sum(axis=1)
    return df_p, df_h

# Processando as duas simulações
df_precipitacao, df_hidrograma = calcular_hidrograma_completo(df_precipitacao)
df_precipitacao_sint, df_hidrograma_sint = calcular_hidrograma_completo(df_precipitacao_sint)

# O arquivo Excel do JOLTEON continua recebendo o evento em tempo real para não quebrar a lógica
df_hidrograma.to_excel(os.path.join(pasta_saida, "hidrograma.xlsx"), index=False)
log_sucesso("Hidrogramas (Tempo Real e Sintético TR-50) calculados com sucesso.")

# ==============================================================================
# 3. MUSKINGUM-CUNGE (ALGORITMO GENÉTICO DEAP + PROPAGAÇÃO DUPLA)
# ==============================================================================
log_inicio("Otimizando Propagação da Onda de Cheia (Muskingum-Cunge) 🧬")

tempo = df_hidrograma['tempo (min)'].values
I = df_hidrograma['Q (m3/s)'].values
delta_t = tempo[1] - tempo[0]
i0 = I.max()
Q_o = i0 * (2/3)
C_o = (5 * math.pow(S_o, 0.3) * math.pow(Q_o, 0.4)) / (3 * math.pow(b_o, 0.4) * math.pow(n_manning, 0.6))
a = (C_o * delta_t * 60) / 2
b = Q_o / (b_o * S_o * delta_t * 60 * C_o * C_o)
delta_x_inicial = a * (1 + math.sqrt(1 + 1.5 * b))

def calcular_delta_x(numero_secoes): return comprimento_rio_m / numero_secoes
def ajustar_X_K(delta_x, Q_o, b_o, S_o, C_o):
    X = 0.5 * (1 - (Q_o / (b_o * S_o * C_o * delta_x)))
    if X < 0: X = 0
    K = delta_x / C_o
    return X, K

def fitness(individual, Q_o, b_o, S_o, C_o, delta_x_inicial, delta_t, comprimento_rio_m):
    numero_secoes = individual[0]
    if numero_secoes < 10: return float('inf'),
    delta_x = calcular_delta_x(numero_secoes)
    X, K = ajustar_X_K(delta_x, Q_o, b_o, S_o, C_o)
    penalizacao = abs(delta_x - delta_x_inicial) * 10
    if delta_x > delta_x_inicial: penalizacao += 1000
    if numero_secoes < 15: penalizacao += 500
    return penalizacao,

creator.create("FitnessMin", base.Fitness, weights=(-1.0,))
creator.create("Individual", list, fitness=creator.FitnessMin)
toolbox = base.Toolbox()
toolbox.register("attr_int", random.randint, 10, 100)
toolbox.register("individual", tools.initRepeat, creator.Individual, toolbox.attr_int, 1)
toolbox.register("population", tools.initRepeat, list, toolbox.individual)
toolbox.register("evaluate", fitness, Q_o=Q_o, b_o=b_o, S_o=S_o, C_o=C_o, delta_x_inicial=delta_x_inicial, delta_t=delta_t, comprimento_rio_m=comprimento_rio_m)
toolbox.register("mutate", tools.mutUniformInt, low=10, up=100, indpb=0.5)
toolbox.register("select", tools.selBest)

population = toolbox.population(n=100)
N_GER = 50
for gen in range(N_GER):
    fitnesses = list(map(toolbox.evaluate, population))
    for ind, fit in zip(population, fitnesses): ind.fitness.values = fit
    offspring = list(map(toolbox.clone, toolbox.select(population, len(population))))
    for mutant in offspring: toolbox.mutate(mutant); del mutant.fitness.values
    fitnesses = list(map(toolbox.evaluate, offspring))
    for ind, fit in zip(offspring, fitnesses): ind.fitness.values = fit
    population[:] = offspring

best_ind = tools.selBest(population, 1)[0]
numero_secoes = best_ind[0]
delta_x_final = calcular_delta_x(numero_secoes)
X_final, K_final = ajustar_X_K(delta_x_final, Q_o, b_o, S_o, C_o)

a_coef = 60 * delta_t / 2
C1 = (- K_final * X_final + a_coef) / (K_final * (1 - X_final) + a_coef)
C2 = (K_final * X_final + a_coef) / (K_final * (1 - X_final) + a_coef)
C3 = (K_final * (1 - X_final) - a_coef) / (K_final * (1 - X_final) + a_coef)

# Função que propaga a onda e INSERE A NASCENTE (Km 0) no array
def propagar_onda(df_in, num_sec, c1, c2, c3):
    df_out = df_in.copy()
    df_out.insert(loc=1, column='I (m3/s)', value=df_out['Q (m3/s)'].values)
    
    # Agora as colunas começam com "I (m3/s)" que é a Sec 00!
    cols_q = ['I (m3/s)'] + [f"Q{i+1}_(m3/s)" for i in range(num_sec)]
    
    for col in cols_q[1:]: df_out[col] = 0.0
    for i in range(1, num_sec+1): df_out.loc[0, f"Q{i}_(m3/s)"] = df_out.loc[0, "I (m3/s)"]
    for j in range(1, num_sec+1):
        source = "I (m3/s)" if j == 1 else f"Q{j-1}_(m3/s)"
        current = f"Q{j}_(m3/s)"
        for k in range(1, len(df_out)):
            df_out.loc[k, current] = (c1 * df_out.loc[k-1, source] + c2 * df_out.loc[k, source] + c3 * df_out.loc[k-1, current])
    return df_out, cols_q

# 1. Propagando a chuva Real
df_final, colunas_Q = propagar_onda(df_hidrograma, numero_secoes, C1, C2, C3)
vazao_exutorio = df_final[colunas_Q[-1]].max()

# 2. Propagando o fantasma da chuva Sintética (TR)
df_final_sint, _ = propagar_onda(df_hidrograma_sint, numero_secoes, C1, C2, C3)
vazao_exutorio_sint = df_final_sint[colunas_Q[-1]].max()

log_sucesso(f"Propagação Dupla concluída. Pico Real: {vazao_exutorio:.1f} m³/s | Pico TR-{TR_REFERENCIA}: {vazao_exutorio_sint:.1f} m³/s")

# ==============================================================================
# 4. MICRO-DISCRETIZAÇÃO E ASSOCIAÇÃO ESPACIAL COM RASTERS SEPARADOS
# ==============================================================================
log_inicio("Lendo MDE, HAND e Limiar para montar a Malha de Risco... 🗺️")

num_secoes = len(colunas_Q)
Qpico = df_final[colunas_Q].max().values[:num_secoes]

gdf_rios    = gpd.read_file(shp_rios)
gdf_bairros = gpd.read_file(shp_bairros)

if gdf_rios.crs.is_geographic:
    gdf_rios    = gdf_rios.to_crs(epsg=31983)
    gdf_bairros = gdf_bairros.to_crs(gdf_rios.crs)

linha_rio = linemerge(gdf_rios.geometry.values)

# 4.0 Gerando a malha do MDE
with rasterio.open(mde_tiff) as src_mde:
    mde_data = src_mde.read(1)
    transform_mde = src_mde.transform
    crs_mde = src_mde.crs
    nodata_mde = src_mde.nodata
    
    if nodata_mde is not None: mask_validos = (mde_data != nodata_mde) & np.isfinite(mde_data)
    else: mask_validos = np.isfinite(mde_data)
        
    rows, cols = np.where(mask_validos)
    step = 10 
    rows = rows[::step]
    cols = cols[::step]
    
    xs, ys = rasterio.transform.xy(transform_mde, rows, cols)
    z_vals = mde_data[rows, cols]

gdf_pontos = gpd.GeoDataFrame({'elevacao_mde': z_vals}, geometry=gpd.points_from_xy(xs, ys), crs=crs_mde)
if gdf_pontos.crs != gdf_rios.crs: gdf_pontos = gdf_pontos.to_crs(gdf_rios.crs)

# 4.1 Lendo HAND e limiar HAND
pts_coords = [(geom.x, geom.y) for geom in gdf_pontos.geometry]
limiar_vals, hand_vals = [], []

with rasterio.open(hand_tiff) as src_hand:
    for val in src_hand.sample(pts_coords, indexes=1):
        hand_vals.append(val[0])   

with rasterio.open(limiar_tiff) as src_limiar:
    for val in src_limiar.sample(pts_coords, indexes=1):
        limiar_vals.append(val[0]) 

# Tratamento de dados -inf: substitui infinitos por um valor alto (10 metros = seguro)
gdf_pontos["hand_m"] = pd.Series(hand_vals).replace([np.inf, -np.inf], np.nan).fillna(10.0)
gdf_pontos["limiar_class"] = pd.Series(limiar_vals).fillna(-1)  
gdf_pontos['hand_valor'] = gdf_pontos['hand_m'] 

log_sucesso("Topografia limpa: Valores infinitos convertidos para cota de segurança.")

# 4.2 Calculo físico da calha (usando a linha real do rio)
PASSO_M = 15  
distancias_50m = np.arange(0, linha_rio.length, PASSO_M)
pts_50m = [linha_rio.interpolate(d) for d in distancias_50m]
gdf_50m = gpd.GeoDataFrame({"id_50m": np.arange(len(distancias_50m)), "dist_m": distancias_50m}, geometry=pts_50m, crs=gdf_rios.crs)

distancias_geo = np.arange(0, linha_rio.length, delta_x_final)
q_vals = Qpico[:len(distancias_geo)]
if len(distancias_geo) > len(q_vals): distancias_geo = distancias_geo[:len(q_vals)]
if len(distancias_geo) > 1:
    f_q = interp1d(distancias_geo, q_vals, kind='linear', fill_value="extrapolate")
    gdf_50m['Qpico_interp'] = f_q(distancias_50m)
else:
    gdf_50m['Qpico_interp'] = q_vals[0] if len(q_vals) > 0 else 0

dados_50m = []
n_urbano = 0.045
for i, d in enumerate(distancias_50m):
    sec_idx = min(int(d // delta_x_final) + 1, num_secoes)
    h_calha_realista = 1.5 + (d / linha_rio.length) * 2.0 
    decliv = max(S_o, 0.001) 
    
    A = b_o * h_calha_realista
    P = b_o + 2 * h_calha_realista
    R = A / P if P > 0 else 0
    Q_limite = (1 / n_urbano) * A * (R**(2/3)) * math.sqrt(decliv)
    dados_50m.append([i, sec_idx, h_calha_realista, Q_limite])

df_50m = pd.DataFrame(dados_50m, columns=["id_50m", "secao_id", "altura_calha_50m", "Q_limite_50m"])
gdf_50m = gdf_50m.merge(df_50m, on="id_50m")
gdf_50m["taxa_ocupacao"] = gdf_50m["Qpico_interp"] / gdf_50m["Q_limite_50m"]

# 4.3 Associação espacial otimizada via cKDTree
gdf_secoes_musk = gpd.GeoDataFrame({"secao_id": np.arange(1, len(distancias_geo) + 1)}, geometry=[linha_rio.interpolate(d) for d in distancias_geo], crs=gdf_rios.crs)

pts_terreno_array = np.array([(geom.x, geom.y) for geom in gdf_pontos.geometry])

tree_musk = cKDTree(np.array([(geom.x, geom.y) for geom in gdf_secoes_musk.geometry]))
_, idx_musk = tree_musk.query(pts_terreno_array)
gdf_pontos["secao_id"] = gdf_secoes_musk.iloc[idx_musk]["secao_id"].values

tree_50m = cKDTree(np.array([(geom.x, geom.y) for geom in gdf_50m.geometry]))
dist_rio, idx_50m = tree_50m.query(pts_terreno_array)
gdf_pontos["id_50m"] = gdf_50m.iloc[idx_50m]["id_50m"].values

gdf_pontos = gdf_pontos.merge(gdf_50m[['id_50m', 'Q_limite_50m', 'Qpico_interp', 'taxa_ocupacao']], on="id_50m", how="left")
gdf_pontos["conectado_rio"] = dist_rio <= 300

log_sucesso("Matriz e topologia calibradas sem ruídos.")


# ==============================================================================
# 5. INTELIGÊNCIA ARTIFICIAL: MATRIZ DE RISCO (CORRIGIDO E SINCRONIZADO)
# ==============================================================================
print("\n" + "█"*85)
print(" 🌩️   JOLTEON - Random Forest 🤖  🌩️".center(85))
print("█"*85)

try:
    rf_model = joblib.load(caminho_modelo_rf)
    colunas_treino = joblib.load(caminho_colunas_rf) 
    log_sucesso("Cérebro do EEVEE e Manual de Colunas sincronizados com sucesso.")
except Exception as e:
    log_alerta(f"Falha ao carregar o cérebro da IA: {e}")
    sys.exit()

agora = datetime.now()
pico_chuva = df_precipitacao['p (mm)'].max() 
acum_24h = df_precipitacao['p (mm)'].tail(24).sum() if len(df_precipitacao) >= 24 else df_precipitacao['p (mm)'].sum()

gdf_pontos['hand_m'] = gdf_pontos['hand_m'].replace([np.inf, -np.inf], np.nan).fillna(10.0)
gdf_pontos['hand_valor'] = gdf_pontos['hand_m'] 

mapa_vazoes_musk = {int(col.replace("Q", "").replace("_(m3/s)", "")): float(df_final[col].max()) for col in df_final.columns if "_(m3/s)" in col}
gdf_pontos['Q_Muskingum_Sim'] = gdf_pontos['secao_id'].map(mapa_vazoes_musk).fillna(0).astype(float)

gdf_pontos['chuva_1h_mm'] = float(pico_chuva)
gdf_pontos['acum_24h'] = float(acum_24h)
gdf_pontos['cn_dinamico'] = 78.0 + ((min(max(agora.year, 2010), 2026)-2010)/16)*12.0

X_matrix = gdf_pontos[colunas_treino].fillna(0)
matriz_probs = rf_model.predict_proba(X_matrix)
n_classes = matriz_probs.shape[1]

gdf_pontos["prob_max"] = np.max(matriz_probs, axis=1)
gdf_pontos["classe_rf"] = np.argmax(matriz_probs, axis=1)

print("\n--- 🕵️ RELATÓRIO DE SAÚDE DA IA ---")
print(f"Número de classes conhecidas pela IA: {n_classes}")

if n_classes > 1:
    prob_alagamento_max = np.max(matriz_probs[:, 1])
    print(f"Probabilidade Máxima de Alagamento: {prob_alagamento_max:.4f}")
else:
    prob_alagamento_max = 0
    log_alerta("IA viciada: Treinada apenas com UMA classe (Verifique o EEVEE).")

print(f"Valores Médios enviados para a IA:")
print(f" -> Chuva: {gdf_pontos['chuva_1h_mm'].mean():.2f} mm")
print(f" -> Vazão (Q): {gdf_pontos['Q_Muskingum_Sim'].mean():.2f} m³/s")

# Correção visual no log para não mostrar -inf
print(f" -> HAND: {gdf_pontos['hand_valor'].replace([np.inf, -np.inf], np.nan).mean():.2f} m")

# --- Logica de risco territorial
gdf_pontos['RISCO_FINAL'] = 0 

# 🚀 Trava removida: Inundação segue puramente a Física e o HAND
mask_fluvial = (gdf_pontos['limiar_class'] >= 0) & (gdf_pontos['taxa_ocupacao'] >= 1.0) & (gdf_pontos['hand_m'] <= 2.5)

# Mascara Pluvial
mask_pluvial = (gdf_pontos['classe_rf'] >= 1) & (~mask_fluvial)

gdf_pontos.loc[mask_pluvial, 'RISCO_FINAL'] = 1  
gdf_pontos.loc[mask_fluvial, 'RISCO_FINAL'] = 2  

log_sucesso(f"Inferência concluída: {mask_pluvial.sum()} pontos pluviais e {mask_fluvial.sum()} pontos fluviais.")

# ============================================================================== 
# 6. RENDERIZAÇÃO GRÁFICA (IA CONTÍNUA E CONVERSÃO DE TEMPO)
# ============================================================================== 
print("\n") 
etapas = ["Formatando Eixos Temporais", "Gerando HTML Hidrológico", "Ancorando Secções GIS", "Concluindo Mapas"] 
with tqdm(total=len(etapas), desc="Renderizando Visualizações", ncols=95, file=sys.stdout, leave=True) as pbar: 
    
    TR_REF = 50 
    
    # 6.1. Conversão de tempo (minutos para HH:MM)
    data_base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    df_precipitacao['hora_plot'] = data_base + pd.to_timedelta(df_precipitacao['tempo (min)'], unit='m')
    df_precipitacao_sint['hora_plot'] = data_base + pd.to_timedelta(df_precipitacao_sint['tempo (min)'], unit='m')
    df_hidrograma['hora_plot'] = data_base + pd.to_timedelta(df_hidrograma['tempo (min)'], unit='m')
    df_hidrograma_sint['hora_plot'] = data_base + pd.to_timedelta(df_hidrograma_sint['tempo (min)'], unit='m')
    df_final['hora_plot'] = data_base + pd.to_timedelta(df_final['tempo (min)'], unit='m')
    df_final_sint['hora_plot'] = data_base + pd.to_timedelta(df_final_sint['tempo (min)'], unit='m')
    pbar.update(1)

    # 6.2 Previsao da IA em funcao do tempo (risco continuo)
    df_risco_t = pd.DataFrame({
        'chuva_1h_mm': [float(pico_chuva)] * len(df_final),
        'Q_Muskingum_Sim': df_final[colunas_Q[-1]], 
        'acum_24h': [float(acum_24h)] * len(df_final),
        'cn_dinamico': [78.0 + ((min(max(agora.year, 2010), 2026)-2010)/16)*12.0] * len(df_final),
        'hand_valor': [1.5] * len(df_final) 
    })
    probs_t = rf_model.predict_proba(df_risco_t[colunas_treino].fillna(0))
    prob_colapso_t = np.max(probs_t, axis=1) * 100 

    # 6.3 HTMLs PLOTLY: chuva + vazao + risco da IA 
    caminho_fig1 = os.path.join(pasta_saida, "JOLTEON_01_HIDROLOGIA_IA.html") 
    fig1 = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.08, 
                         row_heights=[0.3, 0.4, 0.3], 
                         subplot_titles=("Precipitação Efetiva (mm)", "Hidrograma SCS-CN (m³/s)", "Probabilidade de Colapso - IA (%)"))
    
    fig1.add_trace(go.Bar(x=df_precipitacao_sint['hora_plot'], y=df_precipitacao_sint['pe (mm)'], name=f'TR {TR_REF}', marker_color='#e74c3c', opacity=0.3), row=1, col=1) 
    fig1.add_trace(go.Bar(x=df_precipitacao['hora_plot'], y=df_precipitacao['pe (mm)'], name='Tempo Real', marker_color='#3498db', opacity=0.9), row=1, col=1) 
    fig1.update_yaxes(autorange="reversed", row=1, col=1) 
    
    fig1.add_trace(go.Scatter(x=df_hidrograma_sint['hora_plot'], y=df_hidrograma_sint['Q (m3/s)'], name=f'Vazão TR {TR_REF}', line=dict(color='red', width=2, dash='dot', shape='spline', smoothing=1.3)), row=2, col=1) 
    fig1.add_trace(go.Scatter(x=df_hidrograma['hora_plot'], y=df_hidrograma['Q (m3/s)'], name='Vazão Tempo Real', line=dict(color='blue', width=3, shape='spline', smoothing=1.3)), row=2, col=1) 
    
    fig1.add_trace(go.Scatter(x=df_final['hora_plot'], y=prob_colapso_t, name='Risco IA (%)', fill='tozeroy', line=dict(color='purple', width=2, shape='spline')), row=3, col=1)
    
    fig1.update_xaxes(tickformat="%H:%M")
    fig1.update_layout(title=f"<b>FIGURA 1: NOWCASTING & I.A. PREDITIVA</b>", template="plotly_white", height=900, barmode='overlay') 
    fig1.write_html(caminho_fig1) 
    pbar.update(1) 

    # 6.4 Figura 2: Muskingum-Cunge, com todas as secoes
    caminho_fig2 = os.path.join(pasta_saida, "JOLTEON_02_MUSKINGUM.html")
    fig2 = make_subplots(rows=1, cols=2, shared_yaxes=True, subplot_titles=("<b>Onda Real (Todas as Secções)</b>", f"<b>Onda Sintética TR-{TR_REF} (Todas as Secções)</b>"))
    
    qtd_secoes_reais = len(colunas_Q) - 1 
    
    for i, coluna in enumerate(colunas_Q): 
        sec_num = i 
        if sec_num == 0: nome = "Sec 00 (Nascente)"
        elif sec_num == qtd_secoes_reais: nome = f"Sec {sec_num:02d} (Exutório)"
        else: nome = f"Sec {sec_num:02d}"
            
        cor_dinamica = f'hsl({int(360 * (i / len(colunas_Q)))}, 85%, 45%)' 
        
        fig2.add_trace(go.Scatter(x=df_final["hora_plot"], y=df_final[coluna], name=nome, line=dict(width=1 if sec_num in [0, qtd_secoes_reais] else 2, color=cor_dinamica, shape='spline', smoothing=1.3), showlegend=True), row=1, col=1) 
        fig2.add_trace(go.Scatter(x=df_final_sint["hora_plot"], y=df_final_sint[coluna], name=nome, line=dict(width=1 if sec_num in [0, qtd_secoes_reais] else 2, color=cor_dinamica, dash='dot', shape='spline', smoothing=1.3), showlegend=False), row=1, col=2) 
    
    fig2.update_xaxes(tickformat="%H:%M")
    fig2.update_layout(title=f"<b>FIGURA 2: PROPAGAÇÃO DA ONDA METRO A METRO (REAL vs SINTÉTICO)</b>", template="plotly_white", height=600) 
    fig2.write_html(caminho_fig2) 

    # 6.5 Ancoragem fisica das estagas no webgis
    if len(gdf_rios) > 1:
        linha_continua = linemerge(gdf_rios.geometry.tolist())
    else:
        linha_continua = gdf_rios.geometry.iloc[0]

    pontos_musk = []
    ids_musk = []
    
    # Estaca 00: Cravada exatamente na coordenada 0.0 (Nascente)
    pontos_musk.append(linha_continua.interpolate(0))
    ids_musk.append(0)
    
    # Restantes estacas cravadas à distância exata do delta_x (dx)
    for sec in range(1, numero_secoes + 1):
        distancia_acumulada = min(sec * delta_x_final, linha_continua.length)
        pontos_musk.append(linha_continua.interpolate(distancia_acumulada))
        ids_musk.append(sec)
        
    # Substitui a velha malha de pontos pela nova, geometricamente perfeita
    gdf_secoes_musk = gpd.GeoDataFrame({'secao_id': ids_musk}, geometry=pontos_musk, crs=gdf_rios.crs)
    pbar.update(1)

    # 6.6 Mapa matplotlib e webgis
    caminho_mapa_hd = os.path.join(pasta_saida, "JOLTEON_MAPA_ALTA_RESOLUCAO.png")
    caminho_webgis = os.path.join(os.path.expanduser("~"), "Desktop", "JOLTEON_WEBGIS.html")

    possiveis_nomes = ['NM_BAIRRO', 'NOME', 'BAIRRO', 'NM_DISTRI', 'Name', 'nome', 'bairro'] 
    fig3, ax = plt.subplots(1, 1, figsize=(14, 14)) 
    ax.set_facecolor('#f4f6f9') 
    
    gdf_bairros.plot(ax=ax, facecolor="white", edgecolor="silver", linewidth=1.0, alpha=0.7, zorder=1) 
    gdf_rios.plot(ax=ax, color="#1e3799", linewidth=2.0, zorder=4, label="Calha Principal (Rio)") 

    def criar_mancha_detalhada(gdf_subset, buffer_size=25, retract_size=15): 
        if gdf_subset.empty: return None 
        mancha_bruta = gdf_subset.geometry.buffer(buffer_size).unary_union.buffer(-retract_size) 
        if mancha_bruta.is_empty: return None
        def preencher(shape): 
            if shape.geom_type == 'Polygon': return Polygon(shape.exterior) 
            elif shape.geom_type == 'MultiPolygon': return MultiPolygon([Polygon(p.exterior) for p in shape.geoms]) 
            return shape 
        return preencher(mancha_bruta) 

    d_cores = {0: '#3498db', 1: '#f1c40f', 2: '#e67e22', 3: '#c0392b'}
    manchas_f_web = {}

    mask_inundacao = (gdf_pontos['taxa_ocupacao'] >= 1.0)
    for cl in [0, 1, 2, 3]:
        subset_cl = gdf_pontos[mask_inundacao & (gdf_pontos['limiar_class'] == cl)]
        mancha = criar_mancha_detalhada(subset_cl)
        if mancha:
            manchas_f_web[cl] = mancha
            gpd.GeoSeries([mancha], crs=gdf_pontos.crs).plot(ax=ax, color=d_cores[cl], alpha=0.85, zorder=5+cl)

    # Plota as novas estacas calibradas no mapa estático
    ax.scatter(gdf_secoes_musk.geometry.x, gdf_secoes_musk.geometry.y, color='black', marker='X', s=60, edgecolor='white', linewidth=1, zorder=20)

    subset_pluvial = gdf_pontos[mask_pluvial] 
    if not subset_pluvial.empty: 
        amostra_ia = subset_pluvial.sample(frac=0.005, random_state=42)
        ax.scatter(amostra_ia.geometry.x, amostra_ia.geometry.y, color='#8e44ad', s=8, alpha=0.9, linewidths=0, zorder=15) 

    from matplotlib.lines import Line2D
    leg_elems = [
        Line2D([0], [0], color='#c0392b', lw=0, marker='s', markersize=10, label="Risco Extremo (Banda 1 - Classe 3)"),
        Line2D([0], [0], color='#e67e22', lw=0, marker='s', markersize=10, label="Risco Crítico (Banda 1 - Classe 2)"),
        Line2D([0], [0], color='#f1c40f', lw=0, marker='s', markersize=10, label="Risco Alerta (Banda 1 - Classe 1)"),
        Line2D([0], [0], color='#3498db', lw=0, marker='s', markersize=10, label="Calha Transbordada (Banda 1 - Classe 0)"),
        Line2D([0], [0], color='#8e44ad', lw=0, marker='o', markersize=6, label="Alagamento Pluvial (Ocorrências IA)"),
        Line2D([0], [0], color='black', lw=0, marker='X', markersize=8, label="Secções de Controlo (Muskingum)")
    ]
    ax.legend(handles=leg_elems, loc="upper left", bbox_to_anchor=(1.02, 1), title="Tipologia de Impacto JOLTEON", fontsize=11, framealpha=0.9, borderpad=1.0) 
    
    coluna_bairro = next((col for col in possiveis_nomes if col in gdf_bairros.columns), None) 
    if coluna_bairro: 
        for x, y, label in zip(gdf_bairros.geometry.centroid.x, gdf_bairros.geometry.centroid.y, gdf_bairros[coluna_bairro]): 
            ax.text(x, y, str(label), fontsize=8, ha='center', fontweight='bold', color='#7f8c8d', alpha=0.6, zorder=2) 

    ax.set_xticks([]); ax.set_yticks([]) 
    ax.set_title(f"PAINEL INTEGRADO DE RISCO HIDROLÓGICO - {agora.strftime('%H:%M')}", fontsize=15, fontweight='bold', pad=15) 
    plt.tight_layout() 
    fig3.savefig(caminho_mapa_hd, dpi=400, bbox_inches='tight') 

    # 6.7 Restauracao dos marcadores do webgis
    if HAS_FOLIUM: 
        gdf_bairros_wgs = gdf_bairros.to_crs(epsg=4326) 
        centro_lat = gdf_bairros_wgs.geometry.centroid.y.mean() 
        centro_lon = gdf_bairros_wgs.geometry.centroid.x.mean() 
        m = folium.Map(location=[centro_lat, centro_lon], zoom_start=13, tiles='cartodbpositron') 

        for cl, mancha in manchas_f_web.items():
            if mancha:
                js = gpd.GeoSeries([mancha], crs=gdf_pontos.crs).to_crs(epsg=4326).simplify(0.0001).to_json()
                folium.GeoJson(js, name=f"Limiar Fluvial {cl}", style_function=lambda x, c=d_cores[cl]: {'fillColor': c, 'color': c, 'weight': 1, 'fillOpacity': 0.7}).add_to(m)

        if not subset_pluvial.empty: 
            pluvial_wgs = amostra_ia.to_crs(epsg=4326) 
            fg_pluvial = folium.FeatureGroup(name='Alagamento Pluvial (IA)') 
            for _, row in pluvial_wgs.iterrows(): 
                folium.CircleMarker(location=[row.geometry.y, row.geometry.x], radius=3, color='#8e44ad', weight=0, fill=True, fill_opacity=0.9, popup=f"IA: {row['prob_max']*100:.1f}%").add_to(fg_pluvial) 
            fg_pluvial.add_to(m) 
            
        rios_wgs = gdf_rios.to_crs(epsg=4326) 
        folium.GeoJson(rios_wgs.to_json(), name="Calha Principal do Rio", style_function=lambda x: {'color': 'blue', 'weight': 2}).add_to(m) 
        
        # alocacao das secoes ao longo do rio, pelo Muskingum-Cunge
        fg_secoes = folium.FeatureGroup(name='Secções de Controlo (Muskingum)')
        for _, row in gdf_secoes_musk.to_crs(epsg=4326).iterrows():
            # Formata o nome para ficar elegante no pop-up
            id_sec = row['secao_id']
            if id_sec == 0: nome_popup = "00 (Nascente)"
            elif id_sec == numero_secoes: nome_popup = f"{id_sec:02d} (Exutório)"
            else: nome_popup = f"{id_sec:02d}"
            
            folium.Marker(
                location=[row.geometry.y, row.geometry.x], 
                popup=f"<b>Secção {nome_popup}</b><br>Monitoramento Muskingum", 
                icon=folium.Icon(color='black', icon='info-sign')
            ).add_to(fg_secoes)
        fg_secoes.add_to(m)

        bairros_wgs = gdf_bairros.to_crs(epsg=4326)
        folium.GeoJson(bairros_wgs.to_json(), name="Limite da Bacia", style_function=lambda x: {'fillColor': '#ffffff', 'fillOpacity': 0.05, 'color': '#2c3e50', 'weight': 3, 'dashArray': '5, 5'}).add_to(m)

        folium.LayerControl(collapsed=False).add_to(m) 
        m.save(caminho_webgis) 

    pbar.update(1)

# ==========================================================
# 7. DASHBOARD TELEMÉTRICO E EXPORTAÇÃO FINAL (J0LT30N)
# ==========================================================
import time

gdf_pontos['x'] = gdf_pontos.geometry.x
gdf_pontos['y'] = gdf_pontos.geometry.y

try:
    caminho_shp = os.path.join(pasta_saida, "malha_inundacao_JOLTEON.shp")
    gdf_pontos[['x', 'y', 'RISCO_FINAL', 'classe_rf', 'prob_max', 'limiar_class', 'hand_m', 'elevacao_mde', 'geometry']].to_file(caminho_shp, driver="ESRI Shapefile")
except Exception as e: pass

gargalo_critico = gdf_50m['Q_limite_50m'].min() 
secao_gargalo = gdf_50m.loc[gdf_50m['Q_limite_50m'].idxmin(), 'secao_id'] 

seguros = (gdf_pontos['RISCO_FINAL'] == 0).sum()
alag_pluvial = (gdf_pontos['RISCO_FINAL'] == 1).sum()
inund_fluvial = (gdf_pontos['RISCO_FINAL'] == 2).sum()
colapso_misto = (gdf_pontos['RISCO_FINAL'] == 3).sum()

# 7.1 Inteligencia artificial
prob_max_perc = prob_alagamento_max * 100

print("\n" + "█"*140)
print(" 🧠 JOLTEON (NÚCLEO RANDOM FOREST) - DIAGNÓSTICO EM TEMPO REAL 🧠".center(140))
print("█"*140)

if prob_max_perc >= 70.0:
    veredicto = "🚨 ALERTA MÁXIMO: COLAPSO TERRITORIAL IMINENTE 🚨"
elif prob_max_perc >= 40.0:
    veredicto = "🟡 ATENÇÃO: RISCO MODERADO DE ALAGAMENTOS E TRANSBORDOS 🟡"
else:
    veredicto = "🟢 SITUAÇÃO CONTROLADA: CAPACIDADE DE DRENAGEM OPERANTE 🟢"

print(f" | VEREDICTO DA I.A. : {veredicto}")
print(f" | CONFIANÇA (RISCO) : {prob_max_perc:.2f}% de chance de ocorrência de desastre hidrológico na bacia.")
print(" | MOTIVADORES DA DECISÃO (Feature Importance do Evento Atual):")

importances = rf_model.feature_importances_
pesos_features = sorted(zip(colunas_treino, importances), key=lambda x: x[1], reverse=True)
for col, imp in pesos_features:
    print(f" |   -> {col:<18} : {imp*100:>5.2f}% de peso analítico")
print("█"*140 + "\n")

# 7.2 Fisica do rio com Muskingum-Cunge
print("=" * 140)
print(" [B] MECÂNICA HIDRÁULICA E PARÂMETROS DA CALHA OTIMIZADA (MUSKINGUM-CUNGE):")
print(f" | Celeridade da Onda (c): {C_o:>8.3f} m/s    | Fator Armazenamento (K): {K_final:>8.3f} s")
print(f" | Fator de Atenuação (X): {X_final:>8.3f} (dim)  | Passo Espacial (dx): {delta_x_final:>12.2f} m")
print(f" | Coeficientes do Rio   : C1={C1:.3f} | C2={C2:.3f} | C3={C3:.3f}")
print(f" | Gargalo Crítico       : SEC {int(secao_gargalo):02d} (Estrutura suporta o limite de {gargalo_critico:.1f} m³/s)")
print("=" * 140)


# 7.3 Interploaçao para localizar o tempo completo do pico (hh:mm:ss)
def pico_exato(df, coluna_q, coluna_t):
    y = df[coluna_q].values
    x = df[coluna_t].values * 60 # Convertendo tempo de minutos para segundos
    idx = np.argmax(y)
    
    if idx == 0 or idx == len(y) - 1: return x[idx], y[idx]
    
    x1, x2, x3 = x[idx-1], x[idx], x[idx+1]
    y1, y2, y3 = y[idx-1], y[idx], y[idx+1]
    
    denom = (x1 - x2) * (x1 - x3) * (x2 - x3)
    if denom == 0: return x2, y2
    
    A = (x3 * (y2 - y1) + x2 * (y1 - y3) + x1 * (y3 - y2)) / denom
    B = (x3**2 * (y1 - y2) + x2**2 * (y3 - y1) + x1**2 * (y2 - y3)) / denom
    C = (x2 * x3 * (x2 - x3) * y1 + x3 * x1 * (x3 - x1) * y2 + x1 * x2 * (x1 - x2) * y3) / denom
    
    if A >= 0: return x2, y2
    
    x_pico = -B / (2 * A)
    y_pico = A * x_pico**2 + B * x_pico + C
    return x_pico, y_pico

# 7.4 Tempo de chegada e protocolos para evacuacao
print(" [C] TEMPO DE IMPACTO E PROTOCOLOS DE EVACUAÇÃO (COM INTERPOLAÇÃO SUB-HORÁRIA):")
print(f" | {'SEÇÃO':<6} | {'LIMITE':<8} | {'PICO REAL':<10} | {'CHEGADA (H:M:S)':<15} | {'RISCO IA':<8} | {'PICO SINT (TR)':<15} | {'CHEGADA (SINT)':<14} | {'AÇÃO TÁTICA (Defesa Civil)':<26} |")
print(" |" + "-"*136 + "|")

for i, coluna in enumerate(colunas_Q):
    sec_num = i + 1
    sec_id = f"{sec_num:02d}"
    
    # Onda Real
    t_seg, q_pico = pico_exato(df_final, coluna, 'tempo (min)')
    h_pico, m_pico, s_pico = int(t_seg // 3600), int((t_seg % 3600) // 60), int(t_seg % 60)
    
    # Onda Sintética (TR)
    t_sint_seg, q_pico_sint = pico_exato(df_final_sint, coluna, 'tempo (min)')
    h_pico_sint, m_pico_sint, s_pico_sint = int(t_sint_seg // 3600), int((t_sint_seg % 3600) // 60), int(t_sint_seg % 60)
    
    linha_sec = gdf_50m[gdf_50m['secao_id'] == sec_num]
    q_lim = linha_sec['Q_limite_50m'].mean() if not linha_sec.empty else 999.99
    
    # Previsão da I.A. Específica por Seção
    df_sec_risco = pd.DataFrame({
        'chuva_1h_mm': [float(pico_chuva)],
        'Q_Muskingum_Sim': [q_pico],
        'acum_24h': [float(acum_24h)],
        'cn_dinamico': [78.0 + ((min(max(agora.year, 2010), 2026)-2010)/16)*12.0],
        'hand_valor': [1.5]
    })
    risco_sec_ia = np.max(rf_model.predict_proba(df_sec_risco[colunas_treino].fillna(0))) * 100
    
    # Decisão Tática
    tempo_alerta_minutos = t_seg / 60
    if q_pico > q_lim or risco_sec_ia >= 70.0:
        if tempo_alerta_minutos <= 120: 
            acao = f"🚨 EVACUAR EM {int(tempo_alerta_minutos)} MIN"
        else: 
            acao = f"🔴 ALERTA VERMELHO"
    elif q_pico > q_lim * 0.8 or risco_sec_ia >= 40.0: 
        acao = "🟡 MONITORAMENTO TÁTICO"
    else: 
        acao = "🟢 MARGEM SEGURA"
        
    print(f" | SEC {sec_id} | {q_lim:>4.1f} m³/s | {q_pico:>5.1f} m³/s | {h_pico:02d}h {m_pico:02d}m {s_pico:02d}s | {risco_sec_ia:>6.1f} % | TR-{TR_REFERENCIA:<2}: {q_pico_sint:>5.1f} | {h_pico_sint:02d}h {m_pico_sint:02d}m {s_pico_sint:02d}s | {acao:<26} |")

print("=" * 140)
print(" [D] MATRIZ DE RISCO TERRITORIAL (ALVOS ATINGIDOS NA CIDADE - MDE/HAND):")
print(f" | 🟢 {'Áreas Seguras (Calha e Drenagem OK)':<40} | {seguros:>10} pixels mapeados{'':<30}|")
print(f" | 🟡 {'Alagamento Pluvial (Falta de Drenagem)':<40} | {alag_pluvial:>10} pixels mapeados{'':<30}|")
print(f" | 🔵 {'Inundação Fluvial (Transbordamento)':<40} | {inund_fluvial:>10} pixels mapeados{'':<30}|")
print(f" | 🔴 {'Colapso Misto (Onda de Cheia + Chuva)':<40} | {colapso_misto:>10} pixels mapeados{'':<30}|")
print("=" * 140 + "\n")

# Abertura automática dos resultados gráficos
abrir_html(caminho_fig1)
abrir_html(caminho_fig2)
if HAS_FOLIUM and platform.system() == 'Darwin': 
    subprocess.call(['open', caminho_webgis])
elif HAS_FOLIUM:
    webbrowser.open('file://' + os.path.realpath(caminho_webgis))

plt.show()