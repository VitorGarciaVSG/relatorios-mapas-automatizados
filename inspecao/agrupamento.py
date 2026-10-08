# -*- coding: utf-8 -*-
"""Agrupamento e roteirização de pontos em Python puro (sem arcpy).

Separado da toolbox para poder ser testado sem ArcGIS Pro.
`pontos` é sempre um dict {id: (x, y)} em coordenadas projetadas (metros).
"""
import math


def kmeans(pontos, pontos_por_grupo, iteracoes=30):
    """Agrupa os pontos em K grupos, com K = ceil(total / pontos_por_grupo).

    Retorna (grupos, centroides): grupos = {indice: [ids]}.
    Os centroides iniciais são pontos espalhados ao longo do eixo Y, para que o
    resultado seja determinístico (mesma entrada, mesmas páginas).
    """
    k = max(1, math.ceil(len(pontos) / pontos_por_grupo))
    ids_ord = sorted(pontos, key=lambda i: (pontos[i][1], pontos[i][0]))
    passo = max(1, len(ids_ord) // k)
    centroides = [pontos[ids_ord[i * passo]] for i in range(k)]

    grupos = {i: [] for i in range(k)}
    for _ in range(iteracoes):
        grupos = {i: [] for i in range(k)}
        for pid, (x, y) in pontos.items():
            melhor = min(range(k), key=lambda i: (x - centroides[i][0]) ** 2 + (y - centroides[i][1]) ** 2)
            grupos[melhor].append(pid)

        novos = []
        for i in range(k):
            if not grupos[i]:
                novos.append(centroides[i])  # grupo vazio mantém o centroide anterior
            else:
                cx = sum(pontos[p][0] for p in grupos[i]) / len(grupos[i])
                cy = sum(pontos[p][1] for p in grupos[i]) / len(grupos[i])
                novos.append((cx, cy))
        if novos == centroides:
            break
        centroides = novos
    return grupos, centroides


def ordem_norte_sul(centroides):
    """Índices dos grupos do mais ao norte para o mais ao sul (ordem das páginas)."""
    return sorted(range(len(centroides)), key=lambda i: centroides[i][1], reverse=True)


def rota_vizinho_mais_proximo(pontos_grupo):
    """Sequência de visita dentro de um grupo: começa no ponto mais ao norte e
    segue sempre para o vizinho mais próximo ainda não visitado."""
    restantes = dict(pontos_grupo)
    atual = max(restantes, key=lambda p: restantes[p][1])
    x, y = restantes.pop(atual)
    rota = [atual]
    while restantes:
        atual = min(restantes, key=lambda p: (restantes[p][0] - x) ** 2 + (restantes[p][1] - y) ** 2)
        x, y = restantes.pop(atual)
        rota.append(atual)
    return rota
