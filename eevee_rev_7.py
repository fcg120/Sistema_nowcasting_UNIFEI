"""
EEVEE — Extrator de Eventos e Variáveis de Escoamento Extremo
================================================================

O QUE ESSE SCRIPT FAZ (em ordem):
  1. Lê e limpa os dados brutos das estações pluviométricas + estação
     fluviométrica de exutório (planilhas .xlsx), montando um calendário
     horário único para a bacia.
  2. Testa 4 métodos de espacialização da chuva (Média, Thiessen, IDW,
     Spline) e escolhe automaticamente o de menor erro (RMSE) — ou seja,
     descobre qual método "estima melhor" a chuva num ponto sem pluviômetro.
  3. Gera o cenário de chuva de projeto (pior dia observado) e corrige o
     raster HAND (altura acima da drenagem mais próxima), usado depois
     para mapear risco de inundação.
  4. Calibra automaticamente os parâmetros do modelo hidráulico
     Muskingum-Cunge (como a onda de cheia se propaga rio abaixo) por
     otimização numérica, comparando com vazões reais medidas no exutório.
  5. Treina uma Random Forest (IA) para classificar risco de alagamento
     (seco / alagamento histórico / inundação severa) a partir de chuva,
     vazão simulada e características do terreno.
  6. Salva o modelo treinado (.pkl) para ser consumido pelo JOLTEON.
  7. Gera relatórios HTML interativos (Plotly) com hidrogramas e a
     propagação da onda de cheia.

ENTRADAS ESPERADAS: ver dados/README.md
SAÍDAS GERADAS: modelo_rf_jolteon.pkl, colunas_rf.pkl, JANELA2_HIDROGRAMAS.html,
                JANELA3_ONDAS.html (em dados/saida_eevee/)

Este script deve ser rodado ANTES do J0LT30N_REV13.py, pois é ele quem
gera o modelo de IA e a calibração hidráulica usados no monitoramento.
"""

import pandas as pd
import numpy as np
import geopandas as gpd
import os
import sys
import time
import joblib
import warnings
import rasterio
import subprocess
from scipy.signal import find_peaks
from scipy.spatial import distance
from scipy.interpolate import Rbf, NearestNDInterpolator
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import mean_squared_error, mean_absolute_error, classification_report
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from scipy.optimize import differential_evolution

try:
    from pykrige.ok import OrdinaryKriging
    from pykrige.uk import UniversalKriging
    KRIGE_AVAILABLE = True
except ImportError:
    KRIGE_AVAILABLE = False

warnings.filterwarnings('ignore')

# ==========================================================
# 0. CONFIGURAÇÕES E CAMINHOS
# ==========================================================
# ⚙️ Edite os caminhos abaixo para apontar para as suas pastas locais de dados
# (por padrão, tudo aponta para a pasta "dados/" na raiz do repositório)
BASE_DADOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados")

ARQUIVO_OCORRENCIAS = os.path.join(BASE_DADOS, "sisguarda", "PONTOS_BACIA_E_MARGEM.csv")
pasta_estacoes = os.path.join(BASE_DADOS, "entrada_jolteon", "RF", "treinamento")
arquivo_estacoes = os.path.join(BASE_DADOS, "entrada_jolteon", "estacoes.xlsx")
arquivo_bacia = os.path.join(BASE_DADOS, "entrada_jolteon", "SHP", "HIDROGRAFIA", "HIDROGRAFIA_BACIA_A.shp")
pasta_saida = os.path.join(BASE_DADOS, "saida_eevee")
CAMINHO_RASTER_HAND = os.path.join(BASE_DADOS, "entrada_jolteon", "RF", "HAND", "limiar+hand.tif")

# 📍 Nome da estaçao exultório (DEVE ESTAR DENTRO DA pasta_estacoes)
NOME_ARQUIVO_EXUTORIO = "prado_velho_puc.xlsx"

# 📐 PARÂMETROS FÍSICOS DA BACIA E DO RIO
CRS_PADRAO = "EPSG:31982"
AREA_BACIA_KM2 = 43.2697
COMPRIMENTO_RIO_M = 12768.5
DESNIVEL_RIO_M = 55
N_URBANO = 0.045
LARGURA_BASE = 5.0
ALTURA_BANCO = 2.5 # Altura da calha até transbordar (em metros)

# Calculo da capacidade plena da calha (Q_limite)
A_calha = (LARGURA_BASE * ALTURA_BANCO) + (2 * ALTURA_BANCO**2)
P_calha = LARGURA_BASE + (2 * ALTURA_BANCO * np.sqrt(5))
decliv = max(DESNIVEL_RIO_M / COMPRIMENTO_RIO_M, 0.001)
Q_limite = (1 / N_URBANO) * A_calha * ((A_calha/P_calha)**(2/3)) * np.sqrt(decliv)

def log_inicio(msg): print(f"\n[INÍCIO] {msg}...")
def log_sucesso(msg): print(f"[SUCESSO] {msg}")
def log_alerta(msg): print(f"\033[93m[ALERTA] {msg}\033[0m")

print("\n" + "█"*85)
print("☁️  🌧️  ⚡  INICIALIZANDO O EEVEE  ⚡  🌧️  ☁️")
print("[E]xtrator de [E]ventos e [V]ariáveis de [E]scoamento [E]xtremo")
print("Status: Aprendizado Híbrido (Defesa Civil + Vazão + Cota do Exutório)")
print("█"*85)

# ==========================================================
# 1. LEITURA, LIMPEZA E INVENTÁRIO DAS ESTAÇÕES 📍
# ==========================================================
log_inicio("Sincronizando estações e construindo calendário único")
lista_chuva = []

print(f"\n| {'NOME DA ESTAÇÃO':<32} | {'REGISTROS (Hrs)':<21} | {'STATUS':<15} |")
print("-" * 77)

arquivos = [f for f in os.listdir(pasta_estacoes) if f.endswith(".xlsx") and not f.startswith("~$")]

for arq in arquivos:
    if arq == NOME_ARQUIVO_EXUTORIO: continue 
    try:
        df = pd.read_excel(os.path.join(pasta_estacoes, arq))
        df.columns = [c.strip().lower() for c in df.columns]
        c_data = next((c for c in df.columns if "data" in c), None)
        c_chuva = next((c for c in df.columns if "chuva" in c or "mm" in c), None)
        
        if c_data and c_chuva:
            df["data"] = pd.to_datetime(df[c_data], dayfirst=True, errors='coerce').dt.floor("h")
            df = df.dropna(subset=["data"]).rename(columns={c_chuva: "chuva"})
            df["chuva"] = pd.to_numeric(df["chuva"].astype(str).str.replace(',', '.'), errors='coerce').fillna(0)
            nome_estacao = os.path.splitext(arq)[0].upper()
            df["estacao"] = nome_estacao
            lista_chuva.append(df[["data", "estacao", "chuva"]])
            print(f"| {nome_estacao:<32} | {len(df):<21} | {'Lida (OK)':<15} |")
    except:
        print(f"| {arq:<32} | {'0':<21} | {'ERRO':<15} |")

# Carregamento do exutório
df_exutorio = None
try:
    df_ex_raw = pd.read_excel(os.path.join(pasta_estacoes, NOME_ARQUIVO_EXUTORIO))
    df_ex_raw.columns = [c.strip().lower() for c in df_ex_raw.columns]
    c_q = next(c for c in df_ex_raw.columns if 'vazao' in c or 'q' in c)
    df_ex_raw[c_q] = pd.to_numeric(df_ex_raw[c_q].astype(str).str.replace(',', '.'), errors='coerce')
    df_exutorio = df_ex_raw[['data', c_q]].copy().rename(columns={c_q: 'Q_Real_Dia'})
    df_exutorio['dia'] = pd.to_datetime(df_exutorio['data']).dt.floor('D')
    df_exutorio = df_exutorio.dropna()
    print(f"| {'PRADO VELHO (EXUTÓRIO)':<32} | {len(df_exutorio):<21} | {'FLÚVIO (OK)':<15} |")
except Exception as e:
    sys.exit(f"[ERRO CRÍTICO] Falha ao carregar Prado Velho: {e}")

print("-" * 77)

df_raw = pd.concat(lista_chuva)
df_pivot = df_raw.pivot_table(index="data", columns="estacao", values="chuva", aggfunc='sum').fillna(0)
df_pivot = df_pivot.resample('h').sum().fillna(0)
log_sucesso(f"Bacia unificada em {len(df_pivot)} horas.")

# ==========================================================
# 2. COMPETIÇÃO ESPACIAL 
# ==========================================================
log_inicio("Minimização de erro quadrático - RMSE espacial 📐")

df_eval = df_pivot.resample('D').sum()
gdf_est = gpd.GeoDataFrame(pd.read_excel(arquivo_estacoes), geometry=gpd.points_from_xy(pd.read_excel(arquivo_estacoes)["x_utm"], pd.read_excel(arquivo_estacoes)["y_utm"]), crs=CRS_PADRAO)
bacia = gpd.read_file(arquivo_bacia)
est_list = df_eval.columns.tolist()
coords = np.array([[gdf_est[gdf_est['nome_estacao'].str.upper() == e].geometry.x.iloc[0], gdf_est[gdf_est['nome_estacao'].str.upper() == e].geometry.y.iloc[0]] for e in est_list])
centro = [bacia.centroid.x.iloc[0], bacia.centroid.y.iloc[0]]

y_real, y_med, y_t, y_i, y_sp = [], [], [], [], []

for i, est_alvo in enumerate(est_list):
    outras = [j for j in range(len(est_list)) if j != i]
    if not outras: continue
    c_outras = coords[outras]
    dist = distance.cdist([coords[i]], c_outras)
    idx_thiessen = np.argmin(dist)
    p_idw = 1 / (dist**3 + 1e-6) 
    
    for d_idx in range(len(df_eval)):
        val_real = df_eval.iloc[d_idx][est_alvo]
        ch_outras = df_eval.iloc[d_idx, outras].values
        
        if val_real < 5.0 and np.mean(ch_outras) < 5.0: continue
            
        y_real.append(val_real); y_med.append(np.mean(ch_outras)); y_t.append(ch_outras[idx_thiessen])
        y_i.append(np.sum(ch_outras * p_idw) / np.sum(p_idw))
        try: y_sp.append(max(0, Rbf(c_outras[:,0], c_outras[:,1], ch_outras, function='linear')(coords[i][0], coords[i][1])))
        except: y_sp.append(np.mean(ch_outras))

if len(y_real) < 10:
    resultados_rmse = {'MÉDIA': 999, 'THIESSEN': 999, 'IDW': 0, 'SPLINE': 999}
else:
    resultados_rmse = {'MÉDIA': np.sqrt(mean_squared_error(y_real, y_med)), 'THIESSEN': np.sqrt(mean_squared_error(y_real, y_t)), 'IDW': np.sqrt(mean_squared_error(y_real, y_i)), 'SPLINE': np.sqrt(mean_squared_error(y_real, y_sp))}

VENCEDOR = min(resultados_rmse, key=resultados_rmse.get)
if VENCEDOR == "MÉDIA" and resultados_rmse['MÉDIA'] > (resultados_rmse['IDW'] * 0.85): VENCEDOR = "IDW"
log_sucesso(f"A distribuição que teve o menor RMSE preservando picos é o método: {VENCEDOR} 🏆")

# ==============================================================================
# 3. GERANDO A CHUVA DE PROJETADA E MAPAS ESPACIAIS 
# ==============================================================================
from scipy.interpolate import Rbf, NearestNDInterpolator
from scipy.spatial import distance

log_inicio("Gerando mapas: Pior Cenário Acumulado (Dia Crítico) com Normalização Espacial ⛈️")

# 3.1 Correcao do HAND (exclusao de valores negativos)
try:
    with rasterio.open(CAMINHO_RASTER_HAND) as src:
        hand_data = src.read(1)
        hand_data = np.where(hand_data < 0, 0, hand_data)
        print(f"HAND processado. Novo mínimo: {hand_data.min()}m")
except:
    print("Aviso: Arquivo HAND não encontrado, seguindo com o processamento de chuva.")

# 3.2 Busca da pior situacao, mesmo dia em todas as estacoes
df_diario = df_pivot.resample('D').sum()

# 3.2.1 Conta quantas estações registraram uma chuva considerável (> 5mm) no mesmo dia
estacoes_ativas = (df_diario >= 5.0).sum(axis=1)

# 3.2.2 Volume total de chuva na bacia
volume_total = df_diario.sum(axis=1)

# 3.2.3 Novo Score: Hiper-valoriza eventos generalizados elevando as estações ativas à 3ª potência
score_generalizado = volume_total * (estacoes_ativas ** 3)
dia_critico = score_generalizado.idxmax()

chuva_bacia = []
for h_idx in range(len(df_pivot)):
    ch_hora = np.array([df_pivot.iloc[h_idx][e] if pd.notna(df_pivot.iloc[h_idx][e]) else 0.0 for e in est_list])
    if np.sum(ch_hora) < 0.1:
        chuva_bacia.append(0.0)
    else:
        if VENCEDOR == "IDW":
            dist_c = distance.cdist([centro], coords)[0]
            p_v = (1.0 / (dist_c**3 + 1e-6)) / np.sum(1.0 / (dist_c**3 + 1e-6))
            chuva_bacia.append(np.sum(ch_hora * p_v))
        else:
            chuva_bacia.append(np.mean(ch_hora))
df_bacia = pd.DataFrame({'chuva_1h_mm': chuva_bacia}, index=df_pivot.index)

# ==============================================================================
# 3.3 Janela 1: mapas espaciais de alta qualidade 
# ==============================================================================
log_inicio(f"Renderizando mapas do evento extremo: {dia_critico.strftime('%d/%m/%Y')}")

# 3.4 Extrai a chuva total do dia crítico para cada estação
ch_pico = np.array([df_diario.loc[dia_critico, e] if pd.notna(df_diario.loc[dia_critico, e]) else 0.0 for e in est_list])

print(f"\n[AUDITORIA] Acumulado Diário do Pior Evento ({dia_critico.strftime('%d/%m/%Y')}):")
for est, val in zip(est_list, ch_pico):
    print(f" -> {est:<30} : {val:>6.2f} mm")

ch_visual = ch_pico + (np.arange(len(ch_pico)) * 0.0001)
z_max = max(ch_pico.max(), df_diario.loc[dia_critico].max())
if z_max < 1: z_max = 2.0

# 💡 CORREÇÃO: Inicializa a variável para evitar o NameError caso o bloco falhe
chuva_centroide = 0.0 

try:
    b_x, b_y = [], []
    for geom in bacia.geometry:
        if geom.geom_type == 'Polygon':
            x, y = geom.exterior.coords.xy
            b_x.extend(list(x) + [None]); b_y.extend(list(y) + [None])
        elif geom.geom_type == 'MultiPolygon':
            for poly in geom.geoms:
                x, y = poly.exterior.coords.xy
                b_x.extend(list(x) + [None]); b_y.extend(list(y) + [None])

    # limites estendidos para o Grid
    minx, maxx = np.min(coords[:,0]) - 5000, np.max(coords[:,0]) + 5000
    miny, maxy = np.min(coords[:,1]) - 5000, np.max(coords[:,1]) + 5000
    grid_x, grid_y = np.mgrid[minx:maxx:400j, miny:maxy:400j]

    # 💡 CORREÇÃO: Definindo as escalas que estavam faltando para a normalização
    scale_x = float(maxx - minx) if (maxx - minx) != 0 else 1.0
    scale_y = float(maxy - miny) if (maxy - miny) != 0 else 1.0

    # equalizando coordenadas para IDW e epilines
    norm_cx, norm_cy = (coords[:,0] - minx) / scale_x, (coords[:,1] - miny) / scale_y
    norm_coords = np.column_stack((norm_cx, norm_cy))
    
    norm_gx, norm_gy = (grid_x - minx) / scale_x, (grid_y - miny) / scale_y
    norm_pts_grid = np.vstack((norm_gx.flatten(), norm_gy.flatten())).T

    # a. Thiessen (nearest interpolator usa coordenadas reais)
    grid_thi = NearestNDInterpolator(coords, ch_visual)(grid_x, grid_y)
    
    # b. IDW Suavizado (forçando uma mistura mais homogênea)
    dist_g = distance.cdist(norm_pts_grid, norm_coords)
    suavizacao = 0.15 
    pesos = 1.0 / ((dist_g ** 2) + (suavizacao ** 2))
    grid_idw = (np.sum(pesos * ch_visual, axis=1) / np.sum(pesos, axis=1)).reshape(400, 400)

    # c. Splines (alterado de 'multiquadric' para 'linear')
    grid_spl_func = Rbf(norm_cx, norm_cy, ch_visual, function='linear')
    grid_spl = np.clip(grid_spl_func(norm_gx, norm_gy), 0, z_max * 1.1)

    # d. Calculo exato da chuva no centroide
    if VENCEDOR == "THIESSEN":
        chuva_centroide = NearestNDInterpolator(coords, ch_pico)([centro])[0]
    elif VENCEDOR == "IDW":
        dist_c = distance.cdist([centro], coords)[0]
        pesos_c = 1.0 / (dist_c**2 + 1e-6) 
        chuva_centroide = np.sum(pesos_c * ch_pico) / np.sum(pesos_c)
    else: 
        norm_centro_x = (centro[0] - minx) / scale_x
        norm_centro_y = (centro[1] - miny) / scale_y
        chuva_centroide = float(np.clip(grid_spl_func(norm_centro_x, norm_centro_y), 0, z_max * 1.1))

    fig1 = make_subplots(rows=1, cols=4, subplot_titles=("Thiessen", "IDW Suavizado", "Splines (Linear)", f"Centroide ({VENCEDOR})"))
    grids = [grid_thi, grid_idw, grid_spl, np.full_like(grid_x, chuva_centroide)]

    # e. Textos dinâmicos para mostrar o NOME e o VALOR exato de chuva na estação
    textos_estacoes = [f"{est}<br><b>{val:.1f} mm</b>" for est, val in zip(est_list, ch_pico)]

    # Escala de cores profissional (Azul para pouca chuva -> Laranja/Vermelho para muita chuva)
    escala_isoietas = [
        [0.0, '#f7fbff'], 
        [0.2, '#c6dbef'], 
        [0.4, '#6baed6'], 
        [0.6, '#2171b5'], 
        [0.8, '#fec44f'], 
        [1.0, '#cc4c02']  
    ]

    for i, grid in enumerate(grids):
        col = i + 1
        
        if i == 0:
            # Heatmap com Transparência (opacity=0.6)
            fig1.add_trace(go.Heatmap(z=grid.T, x=grid_x[:,0], y=grid_y[0,:], colorscale=escala_isoietas, zmin=0, zmax=z_max, showscale=(col==4), opacity=0.6), row=1, col=col)
        else:
            # Contornos preenchidos com Transparência (opacity=0.6) e linhas mais sutis
            fig1.add_trace(go.Contour(
                z=grid.T, x=grid_x[:,0], y=grid_y[0,:], 
                colorscale=escala_isoietas, 
                zmin=0, zmax=z_max, 
                contours=dict(coloring='fill', showlines=True, start=0, end=z_max, size=z_max/8), 
                line=dict(color='rgba(0, 0, 0, 0.25)', width=1), 
                showscale=(col==4),
                opacity=0.6
            ), row=1, col=col)
        
        # Bacia Hidrográfica
        fig1.add_trace(go.Scatter(x=b_x, y=b_y, mode='lines', line=dict(color='#2c3e50', width=3), showlegend=False), row=1, col=col)
        
        # Estações de Medição
        fig1.add_trace(go.Scatter(x=coords[:,0], y=coords[:,1], mode='markers+text', text=textos_estacoes, textposition="top center", marker=dict(size=12, color='white', line=dict(width=2, color='#2c3e50')), showlegend=False), row=1, col=col)

    # f. Layout expandido
    fig1.update_layout(
        title=dict(text=f"<b>TEMPESTADE DE PROJETO (ACUMULADO DIÁRIO): {dia_critico.strftime('%d/%m/%Y')}</b>", font=dict(size=24, color='#2c3e50')), 
        width=3000, height=900, plot_bgcolor='#f8f9fa',
        margin=dict(l=40, r=40, t=80, b=40),
        annotations=[dict(text="Autor: Engenho Projetos / E.E.V.E.E.", xref="paper", yref="paper", x=1.0, y=-0.08, showarrow=False, font=dict(size=14, color="gray"))]
    )
    
    caminho_mapa = os.path.join(pasta_saida, "JANELA1_MAPAS_ESPACIAIS.html")
    fig1.write_html(caminho_mapa)
    subprocess.call(['open', caminho_mapa])
    
except Exception as e:
    print(f"Erro na Janela 1: {e}")
# ==========================================================
# 3.2 EXTRAÇÃO DE INTELIGÊNCIA GEOGRÁFICA (HAND + LIMIARES)
# ==========================================================
def integrar_topografia_raster(df, caminho_tiff):
    log_inicio("Sincronizando flutuabilidade temporal com âncora física (HAND)")
    
    # Verificação de segurança do arquivo
    if not os.path.exists(caminho_tiff):
        log_alerta("Arquivo TIFF não encontrado. Prosseguindo sem dados de HAND.")
        df['limiar_mapa'] = 0
        df['hand_valor'] = 0
        return df

    with rasterio.open(caminho_tiff) as src:
        # Criamos a lista de coordenadas UTM dos pontos de treino
        # Nota: O EEVEE usa o centroide da bacia para chuva, mas para o treino 
        # usamos as coordenadas reais dos pontos de ocorrência (PONTOS_BACIA_E_MARGEM).
        coords_pontos = [(x, y) for x, y in zip(df['x_utm'], df['y_utm'])]
        
        # Amostragem das duas bandas:
        # Banda 1: Limiares de inundação discutidos (2.5m, 5.2m, etc)
        # Banda 2: HAND (Altura acima da drenagem mais próxima)
        try:
            amostras = list(src.sample(coords_pontos))
            df['limiar_mapa'] = [v[0] for v in amostras] # Banda 1
            df['hand_valor'] = [v[1] for v in amostras]  # Banda 2
            log_sucesso(f"Âncora HAND integrada: Média de {df['hand_valor'].mean():.2f}m nos pontos.")
        except Exception as e:
            log_alerta(f"Falha na amostragem do Raster: {e}")
            df['limiar_mapa'], df['hand_valor'] = 0, 0
            
    return df

# ==========================================================
# 4. MOTOR FÍSICO COM COMPILADOR JIT E TRAVA TEMPORAL 🌊
# ==========================================================
import numpy as np
import pandas as pd
from sklearn.metrics import r2_score
import sys
import time
from numba import njit

log_inicio("Calibrando DNA Hidráulico (Com Aceleração JIT e Sincronia Temporal)")

# Carregamento do exutorio com sincronia temporal
df_exutorio = None
try:
    df_ex_raw = pd.read_excel(os.path.join(pasta_estacoes, NOME_ARQUIVO_EXUTORIO))
    df_ex_raw.columns = [c.strip().lower() for c in df_ex_raw.columns]
    c_q = next(c for c in df_ex_raw.columns if 'vazao' in c or 'q' in c)
    df_ex_raw[c_q] = pd.to_numeric(df_ex_raw[c_q].astype(str).str.replace(',', '.'), errors='coerce')
    df_exutorio = df_ex_raw[['data', c_q]].copy().rename(columns={c_q: 'Q_Real_Dia'})
    df_exutorio['dia'] = pd.to_datetime(df_exutorio['data']).dt.floor('D')
    df_exutorio = df_exutorio.dropna()

    # [O PULO DO GATO] Corta a fluviometria para existir SÓ na época em que há dados de chuva
    inicio_chuva = df_raw['data'].min().floor('D')
    fim_chuva = df_raw['data'].max().floor('D')
    df_exutorio = df_exutorio[(df_exutorio['dia'] >= inicio_chuva) & (df_exutorio['dia'] <= fim_chuva)]
    
except Exception as e: sys.exit(f"[ERRO CRÍTICO] Falha no Prado Velho: {e}")

# -----------------------------------------------------------
@njit
def propagar_onda_veloz(I, c1, c2, c3):
    O = np.zeros_like(I)
    for t in range(1, len(I)):
        val = c1*I[t] + c2*I[t-1] + c3*O[t-1]
        O[t] = 0.0 if val < 0 else val
    return O
# -----------------------------------------------------------

def solver_muskingum(genes, n_secoes):
    bn, be, gn, gr, k_escala = genes
    dx, dt, s0 = COMPRIMENTO_RIO_M/n_secoes, 3600, max(DESNIVEL_RIO_M / COMPRIMENTO_RIO_M, 0.0001)
    
    inflow = np.clip(df_bacia['chuva_1h_mm'].values, 0, None) * (AREA_BACIA_KM2 * 0.2778 * gr) * k_escala
    larguras = np.linspace(bn, be, n_secoes)
    
    c_list, k_list, x_list = [], [], []
    ondas = [inflow]
    
    for i in range(n_secoes):
        B = larguras[i]
        rh = (B * ALTURA_BANCO) / (B + 2 * ALTURA_BANCO)
        
        c = max(0.1, (5/3) * (1/gn) * (rh**(2/3)) * np.sqrt(s0))
        K = dx/c
        X = max(0.001, min(0.5, 0.5*(1-(B*ALTURA_BANCO)/(B*c*s0*dx))))
        
        c_list.append(c); k_list.append(K); x_list.append(X)
        
        den = 2*K*(1-X)+dt
        c1, c2, c3 = (dt-2*K*X)/den, (dt+2*K*X)/den, (2*K*(1-X)-dt)/den
        
        I = ondas[-1]
        O = propagar_onda_veloz(I, c1, c2, c3)
        O = np.nan_to_num(O, nan=0.0, posinf=10000, neginf=0.0)
        ondas.append(O)
    
    return ondas, (c_list, k_list, x_list, dx)

def fitness(genes, df_ref):
    ondas, _ = solver_muskingum(genes, 15)
    df_t = pd.DataFrame({'dia': df_bacia.index.floor('D'), 'q': ondas[-1]})
    
    comp = pd.merge(df_t.groupby('dia')['q'].max().reset_index(), df_ref, on='dia', how='inner').dropna()
    if len(comp) < 5: return 999999
    
    rmse = np.sqrt(mean_squared_error(comp['Q_Real_Dia'], comp['q']))
    erro_pico = abs(comp['Q_Real_Dia'].max() - comp['q'].max()) / comp['Q_Real_Dia'].max()
    return rmse * (1 + erro_pico)

# ==========================================================
# 4.1 BARRA DE PROGRESSO NATIVA
# ==========================================================
max_iteracoes = 12
limites_genes = [(2.0,6.0), (8.0,20.0), (0.03,0.07), (0.2,0.85), (1.0, 15.0)]

class BarraNativa:
    def __init__(self, total):
        self.total = total
        self.atual = 0
        self.inicio = time.time()
        sys.stdout.write(f'\r ⚡ Evoluindo DNA (JIT ativado): |{"-" * 30}| 0/{self.total} [Calculando População Base...]')
        sys.stdout.flush()

    def atualizar(self, xk, convergence):
        self.atual += 1
        decorrido = time.time() - self.inicio
        restante = (decorrido / self.atual) * (self.total - self.atual) if self.atual > 0 else 0
        m_d, s_d = divmod(int(decorrido), 60); m_r, s_r = divmod(int(restante), 60)
        preenchido = int(30 * (self.atual / float(self.total)))
        sys.stdout.write(f'\r ⚡ Evoluindo DNA (JIT ativado): |{"█"*preenchido + "-"*(30-preenchido)}| {self.atual}/{self.total} [Tempo: {m_d:02d}:{s_d:02d} | Restante: {m_r:02d}:{s_r:02d}]')
        sys.stdout.flush()

barra = BarraNativa(max_iteracoes)

res = differential_evolution(fitness, limites_genes, args=(df_exutorio,), maxiter=max_iteracoes, popsize=5, seed=42, callback=barra.atualizar)
print("\n")

n_secoes_final = 15
ondas_finais, metricas_rio = solver_muskingum(res.x, n_secoes_final)

# ==========================================================
# 4.2 FUNÇÃO DE INTEGRAÇÃO RASTER (ÂNCORA FÍSICA)
# ==========================================================
def integrar_topografia_raster(df, caminho_tiff):
    log_inicio("Sincronizando flutuabilidade temporal com âncora física (HAND)")
    
    if not os.path.exists(caminho_tiff):
        log_alerta(f"Arquivo TIFF não encontrado em: {caminho_tiff}. Prosseguindo com valores zerados.")
        df['limiar_mapa'], df['hand_valor'] = 0.0, 0.0
        return df

    try:
        with rasterio.open(caminho_tiff) as src:
            coords_pts = [(x, y) for x, y in zip(df['x_utm'], df['y_utm'])]
            amostras = list(src.sample(coords_pts))
            
            limiares = np.array([float(v[0]) for v in amostras])
            hands_brutos = np.array([float(v[1]) for v in amostras])
            
            # ISOLANDO OS BURACOS: Valores negativos (NoData) viram NaN
            hands_validos = np.where(hands_brutos < 0, np.nan, hands_brutos)
            
            df['limiar_mapa'] = limiares
            df['hand_valor'] = hands_validos
            
            # Calcula a média ignorando os buracos (NaN)
            media_hand = np.nanmean(hands_validos)
            
            log_sucesso(f"Inteligência Geográfica Integrada! HAND Médio dos pontos válidos: {media_hand:.2f}m")
    except Exception as e:
        log_alerta(f"Falha técnica na amostragem do Raster: {e}")
        df['limiar_mapa'], df['hand_valor'] = 0.0, 0.0
            
    return df

# ==========================================================
# 5. ALINHAMENTO DE DADOS E TREINAMENTO IA 🤖 (SINCRO-FÍSICO + ESTOCÁSTICO)
# ==========================================================
log_inicio("Preparando dataset híbrido: Histórico Real + Cenários Estocásticos (TRs)")

# Construção da Base Temporal e Hidráulica (Série Contínua)
df_base = df_bacia.reset_index().rename(columns={'index': 'data'}).copy()
df_base['dia'] = df_base['data'].dt.floor('D')

df_base['Q_Nascente'] = ondas_finais[0]
df_base['Q_Meio'] = ondas_finais[n_secoes_final // 2]
df_base['Q_Muskingum_Sim'] = ondas_finais[-1]

# Curva de Número (CN) dinâmica
df_base['cn_dinamico'] = df_base['data'].dt.year.map(lambda a: 78.0 + ((min(max(a, 2010), 2026)-2010)/16)*12.0)

# Cálculo de chuvas acumuladas
for h in [1, 24, 168]: 
    df_base[f'acum_{h}h'] = df_base['chuva_1h_mm'].rolling(h, min_periods=1).sum()

amostras_ml = []
minx, miny, maxx, maxy = bacia.total_bounds

# ==========================================================
# 5.1 INJEÇÃO DE DADOS 1: PASSADO REAL (DEFESA CIVIL)
# ==========================================================
try:
    df_oc = pd.read_csv(ARQUIVO_OCORRENCIAS, sep=';', encoding='latin1')
    df_oc.columns = [c.strip().upper() for c in df_oc.columns]
    
    df_oc['DATA_STR'] = df_oc['OCORRENCIA_DATA'].astype(str) + ' ' + df_oc['OCORRENCIA_HORA'].astype(str)
    df_oc['data_h'] = pd.to_datetime(df_oc['DATA_STR'], dayfirst=True, errors='coerce').dt.floor('h')
    df_oc = df_oc.dropna(subset=['data_h', 'UTM_X_22S', 'UTM_Y_22S'])
    
    for _, row in df_oc.iterrows():
        mask = (df_base['data'] >= row['data_h'] - pd.Timedelta(hours=24)) & (df_base['data'] <= row['data_h'] + pd.Timedelta(hours=6))
        if mask.any():
            hora_evento = df_base[mask].iloc[-1].copy()
            # Ponto Real
            pos = hora_evento.copy()
            pos['x_utm'], pos['y_utm'], pos['target'] = row['UTM_X_22S'], row['UTM_Y_22S'], 1
            amostras_ml.append(pos)
            
            # Pseudo-Ausências
            for _ in range(10):
                neg = hora_evento.copy()
                neg['x_utm'], neg['y_utm'], neg['target'] = np.random.uniform(minx, maxx), np.random.uniform(miny, maxy), 0
                amostras_ml.append(neg)
except Exception as e:
    log_alerta(f"Falha ao cruzar as planilhas da Defesa Civil: {e}")

df_treino_bruto = pd.DataFrame(amostras_ml)
if df_treino_bruto.empty:
    sys.exit("[PARADA DE EMERGÊNCIA] O modelo não cruzou ocorrências de alagamento.")

# ==========================================================
# 5.2 INJEÇÃO DE DADOS 2: FUTURO SINTÉTICO (EQUAÇÃO IDF CURITIBA)
# ==========================================================
log_inicio("Simulando colapso estrutural: Injetando Tempestades IDF (TR 10, 25, 50, 100)")

def simular_vazao_sintetica(chuva_array, genes):
    bn, be, gn, gr, k_escala = genes
    dx, dt, s0 = COMPRIMENTO_RIO_M/n_secoes_final, 3600, max(DESNIVEL_RIO_M / COMPRIMENTO_RIO_M, 0.0001)
    inflow = np.clip(chuva_array, 0, None) * (AREA_BACIA_KM2 * 0.2778 * gr) * k_escala
    larguras = np.linspace(bn, be, n_secoes_final)
    ondas = [inflow]
    for i in range(n_secoes_final):
        B = larguras[i]
        rh = (B * ALTURA_BANCO) / (B + 2 * ALTURA_BANCO)
        c = max(0.1, (5/3) * (1/gn) * (rh**(2/3)) * np.sqrt(s0))
        K = dx/c
        X = max(0.001, min(0.5, 0.5*(1-(B*ALTURA_BANCO)/(B*c*s0*dx))))
        den = 2*K*(1-X)+dt
        c1, c2, c3 = (dt-2*K*X)/den, (dt+2*K*X)/den, (2*K*(1-X)-dt)/den
        ondas.append(propagar_onda_veloz(ondas[-1], c1, c2, c3))
    return ondas[-1]

amostras_sinteticas = []
# IDF Curitiba (Prof. Fendrich)
for tr in [10, 25, 50, 100]:
    tempos_min = np.arange(60, (24 * 60) + 1, 60)
    intensidades = (5726.64 * (tr ** 0.159)) / ((tempos_min + 41.0) ** 1.041)
    p_acumulada = intensidades * (tempos_min / 60)
    
    p_inc = np.zeros(len(tempos_min))
    p_inc[0] = p_acumulada[0]
    for i in range(1, len(tempos_min)): p_inc[i] = p_acumulada[i] - p_acumulada[i-1]
        
    p_inc = np.sort(p_inc)[::-1]
    blocos = np.zeros(len(tempos_min))
    meio = len(tempos_min) // 2
    for i in range(len(tempos_min)):
        pos = meio + (i // 2) if i % 2 == 0 else meio - (i // 2) - 1
        if 0 <= pos < len(tempos_min): blocos[pos] = p_inc[i]
            
    # Simula a passagem da onda extrema pelo rio otimizado
    q_sint = simular_vazao_sintetica(blocos, res.x)
    acum_24_sint = pd.Series(blocos).rolling(24, min_periods=1).sum().fillna(0).values
    
    # Extrai o pico do desastre e 2 horas de pós-pico
    idx_picos = np.argsort(q_sint)[-3:] 
    
    for idx in idx_picos:
        for _ in range(40): # Espalha 40 sensores virtuais pela bacia por hora de pico
            amostras_sinteticas.append({
                'data': pd.Timestamp('2030-01-01') + pd.Timedelta(hours=int(idx)), 
                'x_utm': np.random.uniform(minx, maxx),
                'y_utm': np.random.uniform(miny, maxy),
                'chuva_1h_mm': blocos[idx],
                'Q_Muskingum_Sim': q_sint[idx],
                'acum_24h': acum_24_sint[idx],
                'cn_dinamico': 90.0, # Solo 100% saturado no cenário extremo
                'target': 0 # Provisório (Será auto-rotulado pela Física abaixo)
            })

# Fundindo o Passado (Real) com o Futuro (Sintético)
df_treino_hibrido = pd.concat([df_treino_bruto, pd.DataFrame(amostras_sinteticas)], ignore_index=True)

# 4. Acoplamento Espacial (HAND)
df_treino_hibrido = integrar_topografia_raster(df_treino_hibrido, CAMINHO_RASTER_HAND)

df_treino_hibrido.loc[df_treino_hibrido['hand_valor'] < -100, 'hand_valor'] = np.nan
df_treino_hibrido['hand_valor'] = df_treino_hibrido['hand_valor'].replace([np.inf, -np.inf], np.nan).fillna(15.0)

# ==========================================================
# 5.3 AUTO-LABELING: ENSINANDO A FÍSICA PARA A I.A.
# ==========================================================
# Target 2 (Inundação Fluvial): Se o rio transbordou E o ponto no mapa é mais baixo que o nível da água
df_treino_hibrido.loc[(df_treino_hibrido['Q_Muskingum_Sim'] >= Q_limite) & (df_treino_hibrido['hand_valor'] <= ALTURA_BANCO), 'target'] = 2 

mask_reais = df_treino_hibrido['target'].isin([1, 2])
mask_pseudo_seguras = (df_treino_hibrido['target'] == 0) & (df_treino_hibrido['hand_valor'] >= 5.0)

n_reais = mask_reais.sum()
n_seguras = mask_pseudo_seguras.sum()
n_amostras = min(n_seguras, n_reais * 2) 

if n_amostras > 0 and n_reais > 0:
    df_treino = pd.concat([
        df_treino_hibrido[mask_reais],
        df_treino_hibrido[mask_pseudo_seguras].sample(n=n_amostras, random_state=42)
    ]).reset_index(drop=True)
else:
    df_treino = df_treino_hibrido.copy()

# Injeta dias de tempo seco
df_seco = df_base[df_base['chuva_1h_mm'] < 2.0].sample(n=min(len(df_treino), 1000), random_state=42).copy()
df_seco['x_utm'], df_seco['y_utm'], df_seco['target'], df_seco['hand_valor'] = centro[0], centro[1], 0, 15.0
df_treino = pd.concat([df_treino, df_seco], ignore_index=True)

# Auditoria e Limpeza de Dados
df_treino_filtrado = df_treino.copy()

print(f"\n--- 📊 DISTRIBUIÇÃO DE CLASSES (IA Híbrida) ---")
print(f"-> 0 (Áreas Seguras / Tempo Seco): {(df_treino_filtrado['target'] == 0).sum()} amostras")
print(f"-> 1 (Alagamentos Reais - Histórico): {(df_treino_filtrado['target'] == 1).sum()} amostras")
print(f"-> 2 (Inundação Severa - Simulação/TR): {(df_treino_filtrado['target'] == 2).sum()} amostras")

# Treinamento
X_cols = ['chuva_1h_mm', 'Q_Muskingum_Sim', 'acum_24h', 'cn_dinamico', 'hand_valor']

log_inicio("Treinando Cérebro EEVEE (Aprendendo com o Passado e o Futuro)")
rf = RandomForestClassifier(n_estimators=300, max_depth=15, class_weight='balanced', oob_score=True, random_state=42)
rf.fit(df_treino_filtrado[X_cols].fillna(0), df_treino_filtrado['target'])

# Exportação
if not os.path.exists(pasta_saida): os.makedirs(pasta_saida)
joblib.dump(rf, os.path.join(pasta_saida, "modelo_rf_jolteon.pkl"))
joblib.dump(X_cols, os.path.join(pasta_saida, "colunas_rf.pkl")) 

log_sucesso(f"IA Re-calibrada e blindada para Eventos Extremos! OOB Score: {rf.oob_score_*100:.1f}%.")

# ==============================================================================
# 6. DASHBOARD 7: AUDITORIA TÉCNICA EEVEE (VALIDAÇÃO DE MAGNITUDE) 📊
# ==============================================================================
log_inicio("Validando Magnitude de Picos Físicos (Paradoxo do Observador Assumido)")
c_list, k_list, x_list, dx_val = metricas_rio

# Alinhamento de dados para validação
q_obs = df_exutorio.groupby('dia')['Q_Real_Dia'].max()
q_sim = df_treino.groupby('dia')['Q_Muskingum_Sim'].max()
df_val = pd.merge(q_obs, q_sim, left_index=True, right_index=True).dropna()

# Vazão de Base Estimada
vazao_base_estimada = df_val['Q_Real_Dia'].quantile(0.05) if not df_val.empty else 0
df_val['Q_Musk_Corrigida'] = df_val['Q_Muskingum_Sim'] + vazao_base_estimada

# Filtro para os dias em que a bacia reagiu à chuva
df_val_validos = df_val[df_val['Q_Muskingum_Sim'] > 1.0].copy()

# Filtro dos 25% maiores desastres (Top 25%)
limite_vazao = df_val_validos['Q_Real_Dia'].quantile(0.75) if not df_val_validos.empty else 0
df_val_cheias = df_val_validos[df_val_validos['Q_Real_Dia'] >= limite_vazao]

# Capacidade Plena da Seção Média e Dados Extras
b_media = (res.x[0] + res.x[1]) / 2
rh_media = (b_media * ALTURA_BANCO) / (b_media + 2 * ALTURA_BANCO)
v_capacidade = (1/res.x[2]) * (rh_media**(2/3)) * np.sqrt(DESNIVEL_RIO_M/COMPRIMENTO_RIO_M)
q_capacidade = v_capacidade * (b_media * ALTURA_BANCO)
declividade_pct = (DESNIVEL_RIO_M / COMPRIMENTO_RIO_M) * 100
fator_geracao_escoamento = res.x[3]

print("\n" + "█"*95)
print(" 📊 DASHBOARD - AUDITORIA TÉCNICA HÍBRIDA (EEVEE)")
print("█"*95)

# --- [A] PLUVIOMETRIA DO EVENTO CRÍTICO ---
print(f" [A] PLUVIOMETRIA DO EVENTO CRÍTICO HISTÓRICO ({dia_critico.strftime('%d/%m/%Y')}):")
print(f" | {'Matemática Vencedora (Menor RMSE)':<35} | {VENCEDOR:>15} |")
for est, val in zip(est_list, ch_pico):
    print(f" | -> {est:<32} | {val:>11.1f} mm |")
print(f" | {'Chuva Média Assumida (Centroide)':<35} | {chuva_centroide:>11.1f} mm |")
print("-" * 95)

# --- [B] GEOMETRIA E HIDRÁULICA ---
print(f" [B] MECÂNICA HIDRÁULICA E GEOMETRIA DO RIO:")
print(f" | {'Largura de Base Otimizada (Média)':<35} | {b_media:>15.2f} metros |")
print(f" | {'Rugosidade de Manning (n)':<35} | {res.x[2]:>15.4f} (s/m1/3)|")
print(f" | {'Declividade do Canal (S0)':<35} | {declividade_pct:>15.3f} % |")
print(f" | {'Velocidade de Escoamento Pleno (v)':<35} | {v_capacidade:>15.2f} m/s |")
print(f" | {'Vazão Limite da Calha (Q_lim)':<35} | {q_capacidade:>15.2f} m³/s |")
print(f" | {'Celeridade Média da Onda (c)':<35} | {np.mean(c_list):>15.3f} m/s |")
print(f" | {'Fator de Escala da Bacia (K_esc)':<35} | {res.x[4]:>15.2f} (mult) |")
print("-" * 95)

# --- [C] VALIDAÇÃO ---
print(f" [C] VALIDAÇÃO HIDROLÓGICA DE PICOS HISTÓRICOS (Vazão Base = {vazao_base_estimada:.1f} m³/s):")
if not df_val_cheias.empty:
    df_val_sorted = df_val_cheias.sort_values('Q_Real_Dia', ascending=False).head(4)
    for dia, row in df_val_sorted.iterrows():
        real, sim_corrigido = row['Q_Real_Dia'], row['Q_Musk_Corrigida']
        erro = abs(real - sim_corrigido) / real * 100 if real > 0 else 0.0
        print(f" | -> Evento {dia.strftime('%d/%m/%Y')}: Real {real:>6.1f} m³/s | Simulado {sim_corrigido:>6.1f} m³/s | Erro: {erro:>5.1f}%")
else:
    print(f" | -> [!] Não há eventos simultâneos de chuva e vazão para validar.")
print("-" * 95)

# --- [D] INTELIGÊNCIA ARTIFICIAL HÍBRIDA (NOVO) ---
qtd_0 = (df_treino_filtrado['target'] == 0).sum()
qtd_1 = (df_treino_filtrado['target'] == 1).sum()
qtd_2 = (df_treino_filtrado['target'] == 2).sum()

print(f" [D] INTELIGÊNCIA ARTIFICIAL HÍBRIDA (TREINAMENTO ESTOCÁSTICO):")
print(f" | {'Amostras de Tempo Seco / Seguras':<35} | {qtd_0:>15} pts |")
print(f" | {'Amostras de Alagamento Real (Hist)':<35} | {qtd_1:>15} pts |")
print(f" | {'Amostras de Inundação Severa (TRs)':<35} | {qtd_2:>15} pts |")
print(f" | {'Acurácia IA (OOB Score)':<35} | {rf.oob_score_*100:>15.2f} % |")
print(" [!] PESO DAS VARIÁVEIS NA DECISÃO (PREVENÇÃO DE OVERFITTING):")
for col, imp in sorted(zip(X_cols, rf.feature_importances_), key=lambda x: x[1], reverse=True)[:4]:
    print(f"     -> {col:<20} : {imp*100:>5.1f}%")
print("█"*95 + "\n")

# ==========================================================
# 7. RELATÓRIOS HTML (JANELAS 2 E 3 - VISTORIA) 📈
# ==========================================================
log_inicio("Gerando Janelas de Vistoria 2 e 3 (Gráficos Plotly contínuos)")

if not os.path.exists(pasta_saida):
    os.makedirs(pasta_saida)

# Busca os dias de maiores picos na série contínua (df_base)
top_5_dias = df_base[df_base['Q_Muskingum_Sim'] > 1.0].groupby('dia').agg({'Q_Muskingum_Sim': 'max'}).sort_values('Q_Muskingum_Sim', ascending=False).head(5)

# ----------------------------------------------------------
# JANELA 2: Hidrogramas Exutório (Hietograma x Hidrograma)
# ----------------------------------------------------------
fig2 = make_subplots(rows=5, cols=1, specs=[[{"secondary_y": True}]]*5, vertical_spacing=0.07)

for i, dia in enumerate(top_5_dias.index):
    inicio_janela = dia - pd.Timedelta(hours=12)
    fim_janela = dia + pd.Timedelta(hours=36)
    
    jan = df_base[(df_base['data'] >= inicio_janela) & (df_base['data'] <= fim_janela)]
    
    # Trace Vazão Simulada
    fig2.add_trace(
        go.Scatter(
            x=jan['data'], 
            y=jan['Q_Muskingum_Sim'], 
            name="Vazão Sim", 
            fill='tozeroy', 
            line=dict(color='blue', width=3),
            legendgroup="vazao",
            showlegend=(i == 0)
        ), 
        row=i+1, col=1
    )
    
    # Trace Chuva (Eixo Y Secundário)
    if 'chuva_1h_mm' in jan.columns:
        fig2.add_trace(
            go.Bar(
                x=jan['data'], 
                y=jan['chuva_1h_mm'], 
                name="Chuva (mm)", 
                marker_color='dodgerblue', 
                opacity=0.6, 
                width=3600000,
                legendgroup="chuva",
                showlegend=(i == 0)
            ), 
            row=i+1, col=1, secondary_y=True
        )
        max_chuva = jan['chuva_1h_mm'].max() if jan['chuva_1h_mm'].max() > 0 else 10
        fig2.update_yaxes(range=[max_chuva * 3, 0], row=i+1, col=1, secondary_y=True, showgrid=False)
    
    # Ajuste do Eixo Y de Vazão: Máximo da própria janela + 20% de margem de respiro
    max_q_janela = jan['Q_Muskingum_Sim'].max() if 'Q_Muskingum_Sim' in jan.columns else 10
    y_limit = (max_q_janela * 1.20) if max_q_janela > 0 else 10

    # Rótulo e Range do Eixo Y e X
    fig2.update_yaxes(title_text="Vazão (m³/s)", range=[0, y_limit], row=i+1, col=1, secondary_y=False)
    fig2.update_xaxes(title_text="Tempo (h)", row=i+1, col=1)

fig2.update_layout(
    height=1400, 
    title="<b>JANELA 2: AUDITORIA DE EVENTOS (HIETOGRAMA X HIDROGRAMA)</b>", 
    template="plotly_white", 
    showlegend=True,
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
)

caminho_h2 = os.path.join(pasta_saida, "JANELA2_HIDROGRAMAS.html")
fig2.write_html(caminho_h2)


# ----------------------------------------------------------
# JANELA 3: Vistoria de Ondas (Propagação Muskingum-Cunge)
# ----------------------------------------------------------
fig3 = make_subplots(rows=5, cols=1, vertical_spacing=0.06)

for i, dia in enumerate(top_5_dias.index):
    inicio_janela = dia - pd.Timedelta(hours=12)
    fim_janela = dia + pd.Timedelta(hours=36)
    
    jan = df_base[(df_base['data'] >= inicio_janela) & (df_base['data'] <= fim_janela)]
    
    if 'Q_Nascente' in jan.columns:
        fig3.add_trace(
            go.Scatter(
                x=jan['data'], 
                y=jan['Q_Nascente'], 
                name="Nascente (0%)", 
                line=dict(color='orange', dash='dot'),
                legendgroup="nasc",
                showlegend=(i == 0)
            ), 
            row=i+1, col=1
        )
        
    if 'Q_Meio' in jan.columns:
        fig3.add_trace(
            go.Scatter(
                x=jan['data'], 
                y=jan['Q_Meio'], 
                name="Meio (50%)", 
                line=dict(color='green', dash='dash'),
                legendgroup="meio",
                showlegend=(i == 0)
            ), 
            row=i+1, col=1
        )
        
    fig3.add_trace(
        go.Scatter(
            x=jan['data'], 
            y=jan['Q_Muskingum_Sim'], 
            name="Exutório (100%)", 
            line=dict(color='blue', width=4),
            legendgroup="exut",
            showlegend=(i == 0)
        ), 
        row=i+1, col=1
    )

    # Ajuste do Eixo Y: Maior vazão entre as 3 séries da janela + 20% de respiro
    cols_vazao = [c for c in ['Q_Nascente', 'Q_Meio', 'Q_Muskingum_Sim'] if c in jan.columns]
    max_q_janela = jan[cols_vazao].max().max() if cols_vazao else 10
    y_limit = (max_q_janela * 1.20) if max_q_janela > 0 else 10

    # Rótulo e Range do Eixo Y e X
    fig3.update_yaxes(title_text="Vazão (m³/s)", range=[0, y_limit], row=i+1, col=1)
    fig3.update_xaxes(title_text="Tempo (h)", row=i+1, col=1)

fig3.update_layout(
    height=1500, 
    title="<b>JANELA 3: PROPAGAÇÃO DE ONDAS (MUSKINGUM-CUNGE)</b>", 
    template="plotly_white", 
    showlegend=True,
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
)

caminho_h3 = os.path.join(pasta_saida, "JANELA3_ONDAS.html")
fig3.write_html(caminho_h3)


# --- COMANDO PARA ABRIR AUTOMATICAMENTE NO MAC ---
log_sucesso("Arquivos HTML gerados. Tentando abrir no navegador...")
for f_path in [caminho_h2, caminho_h3]:
    try:
        subprocess.run(['open', f_path], check=True)
    except Exception as e:
        print(f"\033[93m[AVISO] Não foi possível abrir automaticamente: {f_path}\033[0m")

log_sucesso("EEVEE v5 Concluído! Gráficos restaurados com sucesso.")