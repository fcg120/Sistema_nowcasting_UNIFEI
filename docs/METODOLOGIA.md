# Metodologia detalhada

Este documento explica, passo a passo, as técnicas usadas em cada etapa dos
dois módulos. A ideia é que qualquer pessoa — mesmo sem ler o código — consiga
entender **o que** cada parte faz e **por quê**.

---

## 1. EEVEE — aprendizado histórico

### 1.1 Unificação dos dados de chuva
Cada estação pluviométrica exporta uma planilha própria, com nomes de coluna
e formatos diferentes. O script identifica automaticamente as colunas de
data e de chuva (procurando palavras como "data", "chuva", "mm"), converte
tudo para uma base horária comum e monta uma tabela única
(linhas = horas, colunas = estações).

### 1.2 Espacialização da chuva (qual método "estima melhor"?)
Nem todo ponto da bacia tem um pluviômetro. Por isso, foi testado 4 formas
clássicas de estimar a chuva num ponto a partir das estações vizinhas:

| Método | Ideia central |
|---|---|
| **Média simples** | Média aritmética das estações vizinhas |
| **Thiessen (polígonos)** | Usa o valor da estação mais próxima |
| **IDW** (Inverse Distance Weighting) | Pondera pelo inverso da distância ao cubo — estações mais perto pesam mais |
| **Spline (Rbf)** | Interpolação suave por função de base radial |

Para escolher o melhor, o script faz um teste de **validação cruzada
"leave-one-out"**: para cada estação, finge que ela não existe, estima a
chuva nela usando as outras, e compara com o valor real. O método com menor
**RMSE** (erro quadrático médio) — ou seja, que mais se aproxima da
realidade — é escolhido automaticamente.

### 1.3 Cenário de chuva de projeto + correção do HAND
O script identifica o **dia mais crítico** já registrado (maior volume de
chuva simultânea em várias estações) para servir de cenário de referência,
e corrige o raster **HAND** (altura do terreno acima da drenagem mais
próxima) removendo valores negativos espúrios, que atrapalhariam o mapa de
risco depois.

### 1.4 Otimização do modelo hidráulico (Muskingum-Cunge)
O **Muskingum-Cunge** é um modelo clássico de propagação de onda de cheia
em rios: ele descreve como um pico de vazão que entra na cabeceira do rio
vai se atenuando e se atrasando até chegar ao exutório (ponto de saída da
bacia). Esse modelo depende de parâmetros físicos do rio (largura, declividade,
rugosidade) que raramente são conhecidos com precisão.

O script usa **otimização por evolução diferencial**
(`scipy.optimize.differential_evolution`) para ajustar automaticamente esses
parâmetros, minimizando o erro entre a vazão simulada e a vazão real medida
na estação de exutório, para os eventos históricos disponíveis.

### 1.5 Treinamento da IA (Random Forest)
Para cada instante de tempo, o script monta um "retrato" com variáveis
como chuva na última hora, vazão simulada, chuva acumulada em 24h e
características do terreno (HAND). Esses retratos são rotulados em 3
classes:

- **0** — tempo seco / condição segura
- **1** — alagamento histórico já registrado
- **2** — inundação severa (cenário sintético de tempo de retorno alto)

Uma **Random Forest** (conjunto de árvores de decisão) aprende a associar
o "retrato" das condições à classe de risco. A qualidade do modelo é
reportada pelo **OOB score** (acurácia estimada sem precisar separar um
conjunto de teste à parte) e pela **importância de cada variável** na
decisão — o que ajuda a interpretar *por que* a IA está prevendo risco alto.

### 1.6 Relatórios
Dois relatórios HTML interativos (Plotly) são gerados: hidrograma x
hietograma (chuva) dos 5 maiores eventos, e a propagação da onda em três
pontos do rio (nascente, meio, exutório).

---

## 2. JOLTEON — nowcasting operacional

### 2.1 Hidrograma SCS-CN
A partir da chuva observada, o script calcula a **chuva efetiva** (a
parcela que realmente vira escoamento superficial, descontando infiltração)
pelo método **SCS-CN** (Soil Conservation Service — Curve Number), um método
padrão em hidrologia que depende de um parâmetro (**CN**) relacionado ao
tipo de solo e uso urbano da bacia. O hidrograma resultante é comparado com
uma **tempestade sintética de referência**, construída pela equação
**IDF (Intensidade-Duração-Frequência) de Curitiba (criada por Fendrich, foi utilzada para desenvolvimento do código)** — isso dá
uma régua de comparação: "essa chuva real está próxima de um evento de
Tempo de Retorno de quantos anos?"

### 2.2 Propagação Muskingum-Cunge por seções
Usando os parâmetros ajustados pelo EEVEE, a onda de cheia é propagada
seção por seção ao longo do rio, permitindo estimar a vazão de pico e o
horário de chegada em cada trecho — não só no exutório final.

### 2.3 Classificação espacial de risco
Cada célula do raster (pixel do mapa) recebe uma classificação combinando:
- **HAND** (altura acima da drenagem — proximidade física ao risco de transbordamento);
- **Limiar de inundação** pré-calculado;
- **Probabilidade do Random Forest** treinada pelo EEVEE, aplicada às condições atuais de chuva e vazão.

O resultado classifica cada ponto como: seguro, alagamento pluvial
(falta de drenagem), inundação fluvial (transbordamento do rio) ou
colapso misto (os dois efeitos juntos).

### 2.4 Mapa interativo (WebGIS)
Um mapa Folium reúne: limite da bacia, rede de drenagem, seções de
controle do Muskingum-Cunge e os pontos de risco classificados — navegável
e com camadas que podem ser ligadas/desligadas tendo como base para o mapa o Google Earth Engine.

### 2.5 Painel de decisão
Para cada seção do rio, o script calcula:
- o tempo estimado até o pico de vazão (com interpolação parabólica
  sub-horária, para maior precisão que o passo de tempo bruto);
- se a vazão prevista ultrapassa a capacidade da calha naquela seção;
- a ação recomendada: 🟢 margem segura, 🟡 monitoramento,
  🔴 alerta vermelho, ou 🚨 evacuação com prazo estimado em minutos.

---

## 3. Glossário rápido

| Termo | Significado |
|---|---|
| **HAND** | Height Above the Nearest Drainage — altura do terreno acima do curso d'água mais próximo; quanto menor, maior o risco de inundação |
| **RMSE** | Raiz do erro quadrático médio — mede o quão distante uma estimativa está do valor real |
| **IDW** | Inverse Distance Weighting — interpolação espacial ponderada pela distância |
| **Muskingum-Cunge** | Modelo hidráulico que simula a propagação de uma onda de cheia ao longo de um rio |
| **SCS-CN** | Método padrão para estimar chuva efetiva (escoamento) a partir da chuva total e do tipo de solo/uso do solo |
| **IDF** | Curva Intensidade-Duração-Frequência — relaciona intensidade da chuva, duração e tempo de retorno |
| **TR (Tempo de Retorno)** | Período médio, em anos, entre a ocorrência de eventos de chuva de determinada magnitude |
| **Random Forest** | Algoritmo de aprendizado de máquina que combina várias árvores de decisão para classificar ou prever |
| **OOB score** | Out-of-bag score — estimativa de acurácia de uma Random Forest usando dados não vistos por cada árvore individual |
| **Exutório** | Ponto de saída de uma bacia hidrográfica, onde toda a água drenada converge |
