# Pasta de dados

Esta pasta **não é versionada no Git** (veja `.gitignore`) porque contém arquivos
geoespaciais e planilhas pesadas/sensíveis do estudo de caso.

Para rodar os scripts localmente, recrie esta estrutura com os seus próprios dados:

```
dados/
├── sisguarda/
│   └── PONTOS_BACIA_E_MARGEM.csv
├── entrada_jolteon/
│   ├── estacoes.xlsx
│   ├── dados_hidrograma.xlsx
│   ├── dados_muskingum.xlsx
│   ├── precipitacao_tempo_real.xlsx
│   ├── SHP/
│   │   ├── HIDROGRAFIA/HIDROGRAFIA_BACIA_A.shp (+ .shx/.dbf/.prj)
│   │   ├── BAIRROS_BACIA_BELEM/BAIRROS_BACIA_A.shp
│   │   └── HIDROGRAFIA_RIO/HIDROGRAFIA_BANCO.shp
│   └── RF/
│       ├── treinamento/*.xlsx   (estações pluviométricas + estação exutório)
│       ├── mdt/mde_lidar_final5x5.tif
│       └── HAND/ (output_HAND.tif, limiar_hand.tif, limiar+hand.tif)
├── saida_eevee/     (gerado automaticamente pelo EEVEE)
└── saida_dados/     (gerado automaticamente pelo JOLTEON)
```

Os caminhos no topo de `eevee_rev_7.py` e `J0LT30N_REV13.py` já apontam
para essa estrutura relativa — não é necessário editar nada se os dados
forem colocados aqui.
