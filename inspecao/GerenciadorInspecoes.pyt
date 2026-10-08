# -*- coding: utf-8 -*-
"""Toolbox ArcGIS Pro: gera áreas de inspeção de válvulas para Map Series.

Para cada área (ex.: bairro), agrupa as válvulas por proximidade (K-Means),
define a ordem de visita dentro de cada grupo, desenha o polígono de índice
(convex hull) de cada página e acrescenta tudo às camadas mestre existentes.
Requer ArcGIS Pro (arcpy).
"""
import math
import os
import sys

import arcpy

# A .pyt nem sempre tem a própria pasta no sys.path; garante o import do módulo vizinho.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agrupamento import kmeans, ordem_norte_sul, rota_vizinho_mais_proximo  # noqa: E402

RAIO_REFERENCIA = "150 Meters"


class Toolbox(object):
    def __init__(self):
        self.label = "Gerenciador de Inspeções"
        self.alias = "inspecoes"
        self.tools = [GerarAreaInspecaoTool]


class GerarAreaInspecaoTool(object):
    def __init__(self):
        self.label = "Gerador de Área de Inspeção (Consolidado)"
        self.description = ("Agrupa válvulas por proximidade, define a rota de inspeção e alimenta "
                            "as camadas mestre de pontos e de polígonos do Map Series.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p = arcpy.Parameter
        poly = p(displayName="1. Camada de Polígono (Área)", name="lyr_poly", datatype="GPFeatureLayer",
                 parameterType="Required", direction="Input")
        poly.filter.list = ["Polygon"]
        nome = p(displayName="  ↳ Campo Nome da Área", name="campo_nome_poly", datatype="Field",
                 parameterType="Required", direction="Input")
        nome.parameterDependencies = [poly.name]

        pts = p(displayName="2. Camada de Válvulas", name="lyr_pts", datatype="GPFeatureLayer",
                parameterType="Required", direction="Input")
        pts.filter.list = ["Point"]

        cli = p(displayName="3. Camada de Clientes (Imóveis)", name="lyr_cli", datatype="GPFeatureLayer",
                parameterType="Required", direction="Input")
        cli.filter.list = ["Point"]
        insc = p(displayName="  ↳ Campo Inscrição do Imóvel", name="campo_inscricao", datatype="Field",
                 parameterType="Required", direction="Input")
        insc.parameterDependencies = [cli.name]

        out_pts = p(displayName="4. Camada Alvo para Salvar Válvulas (Existente)", name="out_fc",
                    datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        out_pts.filter.list = ["Point"]
        num = p(displayName="  ↳ Nome do Campo Numeração", name="campo_num", datatype="GPString",
                parameterType="Required", direction="Input")
        num.value = "SEQ_VALV"

        por_pag = p(displayName="5. Válvulas (Média) por Página", name="valvas_por_pag", datatype="GPLong",
                    parameterType="Required", direction="Input")
        por_pag.value = 10

        out_ms = p(displayName="6. Camada Alvo para Salvar Polígonos (Existente)", name="out_map_series",
                   datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        out_ms.filter.list = ["Polygon"]

        ref = p(displayName="7. [Opcional] Camada de Referência Externa (ex.: lotes oficiais)", name="lyr_ref",
                datatype="GPFeatureLayer", parameterType="Optional", direction="Input")
        ref.filter.list = ["Polygon"]
        campo_ref = p(displayName="  ↳ Coluna a copiar da referência", name="campo_ref", datatype="Field",
                      parameterType="Optional", direction="Input")
        campo_ref.parameterDependencies = [ref.name]

        return [poly, nome, pts, cli, insc, out_pts, num, por_pag, out_ms, ref, campo_ref]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        arcpy.env.overwriteOutput = True
        lyr_poly = parameters[0].valueAsText
        campo_nome_poly = parameters[1].valueAsText
        lyr_pts = parameters[2].valueAsText
        lyr_cli = parameters[3].valueAsText
        campo_inscricao = parameters[4].valueAsText
        out_fc = parameters[5].valueAsText
        campo_num = parameters[6].valueAsText
        valvas_por_pag = int(parameters[7].value)
        out_map_series = parameters[8].valueAsText
        lyr_ref = parameters[9].valueAsText if parameters[9].value else None
        campo_ref = parameters[10].valueAsText if parameters[10].value else None

        def garantir_campo(tabela, nome, tipo, tamanho=None):
            if nome.upper() not in [f.name.upper() for f in arcpy.ListFields(tabela)]:
                arcpy.management.AddField(tabela, nome, tipo, field_length=tamanho)

        arcpy.AddMessage("Agrupando válvulas por proximidade...")
        nome_area = "Indefinido"
        try:
            with arcpy.da.SearchCursor(lyr_poly, [campo_nome_poly]) as cur:
                for row in cur:
                    if row[0]:
                        nome_area = str(row[0]).strip()[:140]
                    break
        except Exception:
            pass

        # Imóveis dentro da área, indexados pela coordenada, para achar o mais próximo de cada página.
        clientes = {}
        try:
            arcpy.management.SelectLayerByLocation(lyr_cli, "INTERSECT", lyr_poly)
            with arcpy.da.SearchCursor(lyr_cli, ["SHAPE@X", "SHAPE@Y", campo_inscricao]) as cur:
                for row in cur:
                    if row[0] and row[1]:
                        clientes[(row[0], row[1])] = str(row[2]) if row[2] else "N/A"
            arcpy.management.SelectLayerByAttribute(lyr_cli, "CLEAR_SELECTION")
        except Exception:
            pass

        arcpy.management.SelectLayerByLocation(lyr_pts, "INTERSECT", lyr_poly, selection_type="NEW_SELECTION")
        if int(arcpy.management.GetCount(lyr_pts)[0]) == 0:
            return arcpy.AddError("Nenhuma válvula dentro do polígono.")

        temp_pts = "in_memory/temp_pts"
        arcpy.management.CopyFeatures(lyr_pts, temp_pts)
        arcpy.management.SelectLayerByAttribute(lyr_pts, "CLEAR_SELECTION")

        # Atributo da camada de referência. Camadas com Join ativo quebram o SpatialJoin
        # (nomes "tabela.campo"), por isso a camada é copiada para a memória antes.
        ref_dict = {}
        if lyr_ref and campo_ref:
            arcpy.AddMessage(f"Copiando a coluna '{campo_ref}' da camada de referência...")
            try:
                ref_limpa = "in_memory/ref_limpa"
                temp_sj = "in_memory/temp_ref_sj"
                arcpy.management.SelectLayerByLocation(lyr_ref, "WITHIN_A_DISTANCE", temp_pts,
                                                       RAIO_REFERENCIA, "NEW_SELECTION")
                arcpy.management.CopyFeatures(lyr_ref, ref_limpa)
                arcpy.management.SelectLayerByAttribute(lyr_ref, "CLEAR_SELECTION")

                # Após a cópia, "TABELA.CAMPO" vira "TABELA_CAMPO"; localiza pelo sufixo.
                nome_base = campo_ref.split(".")[-1]
                campos = [f.name for f in arcpy.ListFields(ref_limpa)]
                coluna = next((c for c in campos if c.endswith(nome_base)), campo_ref)

                fm = arcpy.FieldMappings()
                fm.addTable(temp_pts)
                fmap = arcpy.FieldMap()
                fmap.addInputField(ref_limpa, coluna)
                saida = fmap.outputField
                saida.name = "DADO_REF"
                fmap.outputField = saida
                fm.addFieldMap(fmap)

                arcpy.analysis.SpatialJoin(temp_pts, ref_limpa, temp_sj, "JOIN_ONE_TO_ONE", "KEEP_ALL",
                                           fm, "CLOSEST", RAIO_REFERENCIA)
                with arcpy.da.SearchCursor(temp_sj, ["TARGET_FID", "DADO_REF"]) as cur:
                    for row in cur:
                        ref_dict[row[0]] = str(row[1]) if row[1] else ""
                arcpy.management.Delete(temp_sj)
                arcpy.management.Delete(ref_limpa)
            except Exception as e:
                arcpy.AddWarning(f"Aviso: não foi possível copiar o dado de referência: {e}")

        campo_pagina = "PAGINA_MS"
        for tabela in (temp_pts, out_fc):
            garantir_campo(tabela, campo_num, "LONG")
            garantir_campo(tabela, campo_pagina, "LONG")

        campo_oid = arcpy.Describe(temp_pts).OIDFieldName
        sr = arcpy.Describe(temp_pts).spatialReference
        is_geo = sr.type == "Geographic"

        pontos = {}
        with arcpy.da.SearchCursor(temp_pts, [campo_oid, "SHAPE@X", "SHAPE@Y"]) as cur:
            for row in cur:
                if row[1] is not None:
                    pontos[row[0]] = (row[1], row[2])

        grupos, centroides = kmeans(pontos, valvas_por_pag)

        # Continua a numeração de páginas do Map Series já existente (MAX(PAGINA) + 1).
        max_pag = 0
        try:
            if "PAGINA" in [f.name.upper() for f in arcpy.ListFields(out_map_series)]:
                with arcpy.da.SearchCursor(out_map_series, ["PAGINA"]) as cur:
                    for (pag,) in cur:
                        if pag and pag > max_pag:
                            max_pag = pag
        except Exception:
            pass

        # A sequência das válvulas é contínua entre páginas: página 1 = 1..n1, página 2 = n1+1...
        ordem_inspecao = {}           # oid -> (sequência, página)
        seq_coords, seq_oid = {}, {}  # sequência -> coordenada / oid
        paginas_seqs, paginas_pontos = {}, {}
        sequencia, pagina = 1, max_pag + 1
        arcpy.SetProgressor("step", "Roteirizando...", 0, len(pontos), 1)
        for gid in ordem_norte_sul(centroides):
            if not grupos[gid]:
                continue
            rota = rota_vizinho_mais_proximo({oid: pontos[oid] for oid in grupos[gid]})
            paginas_seqs[pagina], paginas_pontos[pagina] = [], []
            for oid in rota:
                ordem_inspecao[oid] = (sequencia, pagina)
                seq_coords[sequencia] = pontos[oid]
                seq_oid[sequencia] = oid
                paginas_seqs[pagina].append(sequencia)
                paginas_pontos[pagina].append(pontos[oid])
                sequencia += 1
                arcpy.SetProgressorPosition()
            pagina += 1
        arcpy.ResetProgressor()

        with arcpy.da.UpdateCursor(temp_pts, [campo_oid, campo_num, campo_pagina]) as cur:
            for row in cur:
                if row[0] in ordem_inspecao:
                    row[1], row[2] = ordem_inspecao[row[0]]
                    cur.updateRow(row)

        arcpy.AddMessage("Acrescentando válvulas à camada alvo...")
        arcpy.management.Append(temp_pts, out_fc, "NO_TEST")
        arcpy.management.Delete(temp_pts)

        for c in ["PAGINA", "QTD_COMP", "VALV_INIC", "VALV_FIM"]:
            garantir_campo(out_map_series, c, "LONG")
        for c, tam in [("NOME_AREA", 150), ("INSCRICAO", 50), ("COORD_GGL", 100), ("URL_QR", 255),
                       ("NUM_PLAN", 50), ("DADO_REF", 150)]:
            garantir_campo(out_map_series, c, "TEXT", tam)

        campos = ["SHAPE@", "PAGINA", "NOME_AREA", "QTD_COMP", "VALV_INIC", "VALV_FIM", "INSCRICAO",
                  "COORD_GGL", "URL_QR", "NUM_PLAN", "DADO_REF"]
        folga = 0.0003 if is_geo else 30  # ~30 m, nas unidades da projeção
        wgs84 = arcpy.SpatialReference(4326)

        arcpy.AddMessage("Desenhando polígonos de índice...")
        with arcpy.da.InsertCursor(out_map_series, campos) as icur:
            for pag, coords in paginas_pontos.items():
                multipoint = arcpy.Multipoint(arcpy.Array([arcpy.Point(x, y) for x, y in coords]), sr)
                geom = multipoint.convexHull()

                # Com 1 ou 2 pontos (ou pontos alinhados) o convex hull vira ponto/linha:
                # usa o retângulo envolvente com uma folga mínima.
                if geom.type != "Polygon":
                    e = multipoint.extent
                    xmin, ymin, xmax, ymax = e.XMin, e.YMin, e.XMax, e.YMax
                    if xmax - xmin < folga:
                        xmin -= folga
                        xmax += folga
                    if ymax - ymin < folga:
                        ymin -= folga
                        ymax += folga
                    geom = arcpy.Polygon(arcpy.Array([
                        arcpy.Point(xmin, ymin), arcpy.Point(xmin, ymax), arcpy.Point(xmax, ymax),
                        arcpy.Point(xmax, ymin), arcpy.Point(xmin, ymin)]), sr)

                v_inic, v_fim = min(paginas_seqs[pag]), max(paginas_seqs[pag])
                x_inic, y_inic = seq_coords[v_inic]

                # Primeiro valor de referência não vazio entre as válvulas da página.
                dado_ref = ""
                for seq in paginas_seqs[pag]:
                    valor = ref_dict.get(seq_oid[seq], "").strip()
                    if valor and valor not in ("None", "Null", "N/A"):
                        dado_ref = valor
                        break

                lat, lon = y_inic, x_inic
                try:
                    pt = arcpy.PointGeometry(arcpy.Point(x_inic, y_inic), sr).projectAs(wgs84)
                    if pt:
                        lat, lon = pt.firstPoint.Y, pt.firstPoint.X
                except Exception:
                    pass

                coord_ggl = f"{lat:.6f}, {lon:.6f}"[:99]
                # "%26" (e não "&") porque o link do Maps vai dentro do parâmetro "data" do QR.
                link_maps = f"https://www.google.com/maps/search/?api=1%26query={lat:.6f},{lon:.6f}"
                # Atenção: a coordenada é enviada a um serviço externo de QR code.
                url_qr = f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={link_maps}"[:250]

                inscricao = "N/A"
                if clientes:
                    mais_proximo = min(clientes.items(),
                                       key=lambda i: (i[0][0] - x_inic) ** 2 + (i[0][1] - y_inic) ** 2)
                    inscricao = str(mais_proximo[1])[:49]

                icur.insertRow([geom, pag, nome_area, len(paginas_seqs[pag]), v_inic, v_fim,
                                inscricao, coord_ggl, url_qr, "", dado_ref])
        return
