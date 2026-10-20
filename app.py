"""Quem sustenta o código? Aplicação do projeto de AED II (UFRN).

Analisa a dependência de arquivos em relação a desenvolvedores em projetos open source, com um grafo
bipartido autor–arquivo. Traz projetos já analisados e aceita o link de qualquer repositório público do GitHub.
"""
from html import escape

import networkx as nx
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import analise as an

VERDE, VERDE_ESC = "#2e7d4f", "#1f5c38"
AZUL_ARQ, LARANJA, VERM, CINZA = "#6b7a8f", "#d9822b", "#c0392b", "#8a8f98"
# paleta categórica validada (ordem fixa) para gráficos com várias séries
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
LIMITES = {"1.000": 1000, "2.000": 2000, "5.000": 5000, "10.000": 10000, "20.000": 20000, "Todos": None}

st.set_page_config(page_title="Quem sustenta o código?", page_icon="🌿", layout="wide")
st.markdown(
    """
    <style>
    h1, h2, h3 { color: #1f5c38; }
    div[data-testid="stMetric"] { background: #ffffff; border: 1px solid #dbe7df; border-left: 4px solid #2e7d4f;
        border-radius: 8px; padding: 12px 16px; }
    div[data-testid="stMetricLabel"] p { color: #4b5d52; }
    .legenda { color: #4b5d52; font-size: 0.92rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def pct(x, casas=1):
    return f"{x:.{casas}f}%".replace(".", ",")


def num(x, casas=2):
    return f"{x:.{casas}f}".replace(".", ",")


def milhar(n):
    return f"{int(n):,}".replace(",", ".")


def titulo(texto):
    st.markdown(f"#### {texto}")


def legenda(html):
    st.markdown(f"<p class='legenda'>{html}</p>", unsafe_allow_html=True)


def precisa_reanalisar(o_que):
    st.info(f"Este projeto foi analisado por uma versão anterior e não tem {o_que}. "
            "Use **Analisar outro projeto** na barra lateral com o mesmo repositório para refazer a análise.")


# ----------------------------------------------------------------------------- dados
@st.cache_data(show_spinner=False)
def carregar_projeto(slug, mtime):
    return an.carregar(an.PASTA_PROJETOS / slug)


@st.cache_data(show_spinner=False)
def sucessores_cache(slug, mtime, min_commits, limiar):
    d = carregar_projeto(slug, mtime)
    mf = d["mf"].copy(); mf["critico"] = an.marcar_criticos(mf, min_commits, limiar)
    return an.sucessores(d["arestas"], mf, d["ma"])


def subgrafo_visual(G, limite=1200, n_autores=120, n_arquivos=700):
    """Redes grandes ficam ilegíveis (e lentas) no navegador: desenha só os autores mais conectados e os arquivos mais alterados."""
    if G.number_of_nodes() <= limite:
        return G
    autores = sorted((n for n in G if n.startswith("a:")), key=G.degree, reverse=True)[:n_autores]
    candidatos = {f for a in autores for f in G[a]}
    arquivos = sorted(candidatos, key=lambda f: G.degree(f, weight="weight"), reverse=True)[:n_arquivos]
    S = G.subgraph(set(autores) | set(arquivos)).copy()
    S.remove_nodes_from([n for n in list(S) if S.degree(n) == 0])
    return S


@st.cache_resource(show_spinner="Montando o grafo…")
def construir_grafo(slug, mtime):
    arestas = carregar_projeto(slug, mtime)["arestas"]
    G = nx.Graph()
    for r in arestas.itertuples():
        G.add_edge("a:" + r.autor, "f:" + r.arquivo, weight=r.commits)
    visual = subgrafo_visual(G)
    pos = nx.spring_layout(visual, seed=42, iterations=120 if visual.number_of_nodes() < 800 else 80, weight=None)
    return G, visual, pos


def figura_grafo(H, pos, ma_d, mf_d, removidos=frozenset(), orfaos=frozenset(), criticos=frozenset(), altura=680):
    rem = {"a:" + a for a in removidos}
    orf = {"f:" + f for f in orfaos}
    vx, vy, mx, my = [], [], [], []
    for u, v in H.edges():
        ax, ay = (mx, my) if (u in rem or v in rem) else (vx, vy)
        ax += [pos[u][0], pos[v][0], None]
        ay += [pos[u][1], pos[v][1], None]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=vx, y=vy, mode="lines", hoverinfo="skip", showlegend=False,
                             line=dict(width=0.4, color="rgba(110,125,118,0.35)")))
    if mx:
        fig.add_trace(go.Scatter(x=mx, y=my, mode="lines", hoverinfo="skip", showlegend=False,
                                 line=dict(width=0.5, color="rgba(192,57,43,0.25)")))

    autores = [n for n in H if n.startswith("a:")]
    arquivos = [n for n in H if n.startswith("f:")]
    top = set(sorted(autores, key=H.degree, reverse=True)[:8])

    def hover_autor(n):
        r = ma_d[n[2:]]
        extra = ""
        if "core_proj" in r:
            extra = f"<br>core na projeção: {int(r['core_proj'])}" + (" (núcleo)" if r.get("nucleo") else "")
        if "ativo" in r:
            extra += f"<br>último commit: {r['ultimo_commit']}" + ("" if r["ativo"] else " (inativo)")
        return (f"<b>{escape(n[2:])}</b><br>arquivos tocados: {int(r['arquivos_tocados'])}<br>commits: {int(r['commits'])}"
                f"<br>betweenness: {r['betweenness']:.3f}<br>core: {int(r['core_number'])}"
                f"<br>arquivos críticos: {int(r['arquivos_criticos'])}{extra}")

    def hover_arq(n):
        r = mf_d[n[2:]]
        extra = f"<br>autores ativos: {int(r['autores_ativos'])}" if "autores_ativos" in r else ""
        return (f"<b>{escape(n[2:])}</b><br>autores: {int(r['n_autores'])}<br>commits: {int(r['commits'])}"
                f"<br>autor dominante: {escape(str(r['autor_dominante']))} ({r['fatia_dominante'] * 100:.0f}%)"
                f"<br>core: {int(r['core_number'])}{extra}")

    def add(nos, nome, cor, simbolo, tamanho, hover, texto=None):
        if not nos:
            return
        fig.add_trace(go.Scatter(
            x=[pos[n][0] for n in nos], y=[pos[n][1] for n in nos], mode="markers+text" if texto else "markers", name=nome,
            marker=dict(color=cor, symbol=simbolo, size=[tamanho(n) for n in nos], line=dict(width=0.6, color="white")),
            text=[texto(n) for n in nos] if texto else None, textposition="top center",
            textfont=dict(size=11, color="#1d2b23"), hovertext=[hover(n) for n in nos], hoverinfo="text"))

    tam_a = lambda n: 6 + 2.2 * np.sqrt(H.degree(n))
    add([n for n in autores if n not in rem], "Autor", VERDE, "circle", tam_a, hover_autor, lambda n: n[2:] if n in top else "")
    add([n for n in autores if n in rem], "Autor removido", VERM, "x", tam_a, hover_autor)
    crit = [n for n in arquivos if n not in orf and n[2:] in criticos]
    normais = [n for n in arquivos if n not in orf and n[2:] not in criticos]
    add(normais, "Arquivo", AZUL_ARQ, "square", lambda n: 6, hover_arq)
    add(crit, "Arquivo crítico", LARANJA, "square", lambda n: 8, hover_arq)
    add([n for n in arquivos if n in orf], "Arquivo órfão", VERM, "square", lambda n: 9, hover_arq)

    fig.update_layout(height=altura, margin=dict(l=0, r=0, t=30, b=0), plot_bgcolor="white", paper_bgcolor="rgba(0,0,0,0)",
                      legend=dict(orientation="h", y=1.04, x=0), hoverlabel=dict(bgcolor="white", font_size=12),
                      xaxis=dict(visible=False), yaxis=dict(visible=False, scaleanchor="x"), dragmode="pan")
    return fig


def simular(G, removidos):
    rem = {"a:" + a for a in removidos}
    H = G.copy()
    H.remove_nodes_from(rem)
    orf = {n for n in H if n.startswith("f:") and H.degree(n) == 0}
    H.remove_nodes_from(orf)
    if H.number_of_nodes():
        comps = list(nx.connected_components(H))
        return {n[2:] for n in orf}, max(len(c) for c in comps) / H.number_of_nodes(), len(comps)
    return {n[2:] for n in orf}, 0.0, 0


def perda_conhecimento(arestas, removidos, limiar=an.LIMIAR_COBERTURA):
    """Arquivos em que os autores que ficaram somam menos de `limiar` dos commits (versão ponderada de "órfão")."""
    tot = arestas.groupby("arquivo").commits.sum()
    resto = arestas[~arestas.autor.isin(removidos)].groupby("arquivo").commits.sum().reindex(tot.index).fillna(0)
    return set(tot.index[resto < limiar * tot - 1e-9])


def figura_curvas(curva, n_trials):
    series = [("orfaos_aleatoria_pct", f"Aleatória (média de {n_trials})", CINZA, "dash"),
              ("orfaos_direcionada_pct", "Por grau", SERIES[0], "solid"),
              ("orfaos_betweenness_pct", "Por betweenness na projeção", SERIES[1], "solid"),
              ("orfaos_gulosa_pct", "Gulosa (recalcula a cada saída)", SERIES[2], "solid"),
              ("conhecimento_direcionada_pct", f"Por grau, perda de conhecimento (<{int(an.LIMIAR_COBERTURA * 100)}% dos commits)",
               SERIES[0], "dot")]
    fig = go.Figure()
    for col, nome, cor, traco in series:
        if col in curva:
            fig.add_trace(go.Scatter(x=curva.pct_autores_removidos, y=curva[col], name=nome, mode="lines",
                                     line=dict(color=cor, width=2, dash=traco),
                                     hovertemplate="%{x:.2f}% dos autores → %{y:.1f}% dos arquivos<extra>" + nome + "</extra>"))
    fig.update_layout(template="plotly_white", height=400, xaxis_title="% de autores removidos", yaxis_title="% de arquivos",
                      legend=dict(orientation="h", y=-0.25, x=0), hovermode="x unified", margin=dict(t=20))
    return fig


# ----------------------------------------------------------------------------- barra lateral
projetos = an.listar_projetos()
analisar_clicado = False
with st.sidebar:
    st.header("Projeto")
    if projetos:
        slugs = [p["slug"] for p in projetos]
        rotulos = {p["slug"]: p["repo"] + ("" if p.get("padrao") else " (seu)") for p in projetos}
        ini = st.session_state.get("ultimo")
        escolhido = st.selectbox("Projeto analisado", slugs, index=slugs.index(ini) if ini in slugs else 0,
                                 format_func=lambda s: rotulos[s])
    else:
        escolhido = None
        st.info("Nenhum projeto analisado ainda. Use a caixa abaixo.")

    with st.expander("Analisar outro projeto", expanded=not projetos):
        entrada = st.text_input("Link do GitHub ou dono/repositório", placeholder="https://github.com/pallets/jinja")
        limite_txt = st.select_slider("Commits mais recentes a analisar", options=list(LIMITES), value="5.000",
                                      help="Repositórios grandes demoram. Limitar acelera a análise.")
        analisar_clicado = st.button("Analisar projeto", type="primary", width="stretch")
        st.caption("Só repositórios públicos do GitHub. O histórico é lido sem baixar os arquivos e o clone temporário é apagado. "
                   "Se o projeto já existe, a análise é refeita.")

    st.header("Arquivo crítico")
    limiar = st.slider("Concentração mínima do autor dominante", 50, 95, 80, step=5, format="%d%%") / 100
    min_commits = st.slider("Mínimo de commits do arquivo", 1, 10, 3)
    st.caption("Um arquivo é crítico quando tem pelo menos esse número de commits e um autor concentra essa fatia deles.")

# ----------------------------------------------------------------------------- análise sob demanda
if analisar_clicado:
    try:
        dono, repo, _ = an.normalizar_entrada(entrada)
        existente = next((p for p in projetos if p["slug"] == an.slug(dono, repo)), None)
        with st.status(f"Analisando {dono}/{repo}…", expanded=True) as status:
            barra = st.progress(0.0, text="Iniciando…")
            novo = an.analisar_repositorio(entrada, LIMITES[limite_txt], padrao=bool(existente and existente.get("padrao")),
                                           progress=lambda f, m: barra.progress(min(f, 1.0), text=m))
            status.update(label="Análise concluída", state="complete", expanded=False)
        st.session_state["ultimo"] = novo
        concluido = True
    except an.ErroAnalise as e:
        st.error(str(e)); concluido = False
    except Exception as e:  # nunca derrubar o app por causa de um repositório estranho
        st.error(f"Algo inesperado aconteceu ({type(e).__name__}). Tente outro repositório ou reduza o número de commits.")
        concluido = False
    if concluido:
        st.rerun()

# ----------------------------------------------------------------------------- cabeçalho
st.title("Quem sustenta o código?")
if escolhido is None:
    st.markdown("Cole o link de um repositório do GitHub na barra lateral para começar.")
    st.stop()

info = next(p for p in projetos if p["slug"] == escolhido)
d = carregar_projeto(escolhido, info["mtime"])
G, visual, pos = construir_grafo(escolhido, info["mtime"])
R = d["resumo"]
ma, curva = d["ma"].copy(), d["curva"]
mf = d["mf"].copy()
mf["critico"] = an.marcar_criticos(mf, min_commits, limiar)
ma["arquivos_criticos"] = ma.autor.map(mf[mf.critico].autor_dominante.value_counts()).fillna(0).astype(int)
criticos = set(mf.loc[mf.critico, "arquivo"])
n_a, n_f, n_e = len(ma), len(mf), len(d["arestas"])
ma_d = ma.set_index("autor").to_dict("index")
mf_d = mf.set_index("arquivo").to_dict("index")
elegiveis = int((mf.commits >= min_commits).sum())
n_crit = int(mf.critico.sum())
tem_proj = "core_proj" in ma
tem_tempo = "ativo" in ma

st.markdown(f"**Dependência de arquivos em relação a desenvolvedores** · [{R['repo']}]({R['url']}) · "
            f"AED II · UFRN")
st.caption(f"{milhar(R['funil_commits_lidos'])} commits lidos (sem merges) · versão `{R['head_sha'][:7]}` · coletado em {R['coletado_em']}")
if R.get("limite_commits"):
    st.warning(f"Foram analisados apenas os {milhar(R['limite_commits'])} commits mais recentes de "
               f"{milhar(R['commits_sem_merges_total'])}. Os números descrevem esse recorte, não o histórico inteiro.")

tab_grafo, tab_painel, tab_analises, tab_sim, tab_comp = st.tabs(
    ["Grafo bipartido", "Painel", "Análises da rede", "Simulação de remoção", "Comparar projetos"])

# ----------------------------------------------------------------------------- 1. grafo
with tab_grafo:
    st.subheader("Rede de autores e arquivos")
    legenda("Cada <b>círculo verde</b> é um autor e cada <b>quadrado</b> é um arquivo. Uma linha indica que o autor "
            "alterou o arquivo. O tamanho do autor cresce com o número de arquivos que ele tocou. Arquivos em "
            "<b style='color:#d9822b'>laranja</b> são críticos. Passe o mouse para ver os detalhes, role para ampliar e arraste para mover.")
    if visual is not G:
        n_vis_a = sum(n.startswith("a:") for n in visual)
        st.info(f"Rede grande ({milhar(n_a)} autores e {milhar(n_f)} arquivos): o desenho mostra os {n_vis_a} autores mais conectados "
                f"e os arquivos mais alterados ligados a eles. Painel, simulação e comparação usam a rede completa.")
    st.plotly_chart(figura_grafo(visual, pos, ma_d, mf_d, criticos=criticos), width="stretch",
                    config={"scrollZoom": True, "displaylogo": False}, key="grafo_principal")

# ----------------------------------------------------------------------------- 2. painel
with tab_painel:
    st.subheader("Indicadores da rede")
    principal = ma.sort_values("arquivos_tocados", ascending=False).iloc[0]
    c = st.columns(4)
    c[0].metric("Autores", milhar(n_a)); c[1].metric("Arquivos", milhar(n_f))
    c[2].metric("Ligações", milhar(n_e)); c[3].metric("Densidade bipartida", pct(n_e / (n_a * n_f) * 100, 2))
    c = st.columns(4)
    c[0].metric("Arquivos de autor único", pct((mf.n_autores == 1).mean() * 100))
    c[1].metric("Autores com 1 só arquivo", pct((ma.arquivos_tocados == 1).mean() * 100))
    c[2].metric("Arquivos críticos", f"{n_crit} ({pct(n_crit / elegiveis * 100) if elegiveis else '—'})",
                help=f"Arquivos com pelo menos {min_commits} commits em que um autor tem {int(limiar * 100)}% ou mais deles. "
                     f"Percentual sobre os {elegiveis} arquivos com pelo menos {min_commits} commits.")
    c[3].metric("Arquivos tocados pelo autor principal", pct(principal.arquivos_tocados / n_f * 100),
                help=f"Autor com mais arquivos: {principal.autor}")
    if tem_tempo:
        c = st.columns(4)
        c[0].metric("Autores ativos", f"{milhar(R['n_autores_ativos'])} ({pct(R['n_autores_ativos'] / n_a * 100)})",
                    help=f"Fizeram algum commit nos {R['janela_dias']} dias anteriores a {R['data_referencia']} (commit mais recente).")
        c[1].metric("Arquivos sem nenhum autor ativo", pct((mf.autores_ativos == 0).mean() * 100),
                    help="Ninguém que alterou o arquivo continua ativo: o arquivo já está órfão hoje, sem simular nada.")
        c[2].metric("Críticos com dono inativo", f"{int((mf.critico & ~mf.dono_ativo).sum())} de {n_crit}")
        c[3].metric("Autor principal", principal.autor + ("" if ma_d[principal.autor]["ativo"] else " (inativo)"))

    st.markdown("#### Quanto o conhecimento é compartilhado")
    col1, col2 = st.columns(2)
    bins_a = pd.cut(mf.n_autores, [0, 1, 2, 3, 5, 10, 10**6], labels=["1", "2", "3", "4 a 5", "6 a 10", "mais de 10"])
    d1 = bins_a.value_counts().reindex(["1", "2", "3", "4 a 5", "6 a 10", "mais de 10"]).reset_index()
    d1.columns = ["autores_no_arquivo", "arquivos"]
    f1 = px.bar(d1, x="autores_no_arquivo", y="arquivos", text="arquivos", color_discrete_sequence=[VERDE],
                title="Autores por arquivo", template="plotly_white")
    f1.update_layout(xaxis_title="autores que alteraram o arquivo", yaxis_title="número de arquivos", height=340)
    col1.plotly_chart(f1, width="stretch", key="dist_autores_por_arquivo")
    ordem = ["1", "2", "3 a 5", "6 a 10", "11 a 50", "mais de 50"]
    bins_b = pd.cut(ma.arquivos_tocados, [0, 1, 2, 5, 10, 50, 10**6], labels=ordem)
    d2 = bins_b.value_counts().reindex(ordem).reset_index()
    d2.columns = ["arquivos_do_autor", "autores"]
    f2 = px.bar(d2, x="arquivos_do_autor", y="autores", text="autores", color_discrete_sequence=[VERDE_ESC],
                title="Arquivos por autor", template="plotly_white")
    f2.update_layout(xaxis_title="arquivos que o autor alterou", yaxis_title="número de autores", height=340)
    col2.plotly_chart(f2, width="stretch", key="dist_arquivos_por_autor")

    st.markdown("#### Autores mais conectados")
    cols_top = ["autor", "arquivos_tocados", "commits", "betweenness", "core_number", "arquivos_criticos"] + \
               (["ultimo_commit"] if tem_tempo else [])
    topo = ma.sort_values("arquivos_tocados", ascending=False).head(10)[cols_top]
    st.dataframe(topo, hide_index=True, width="stretch",
                 column_config={"autor": "Autor", "arquivos_tocados": "Arquivos tocados", "commits": "Commits (grau ponderado)",
                                "betweenness": st.column_config.NumberColumn("Betweenness (bipartido)", format="%.3f"),
                                "core_number": "Core", "arquivos_criticos": "Arquivos críticos", "ultimo_commit": "Último commit"})

    st.markdown("#### Arquivos críticos")
    if n_crit:
        crit = mf[mf.critico].sort_values("commits", ascending=False).head(15).copy()
        crit["fatia_dominante"] = crit.fatia_dominante * 100
        cols_c = ["arquivo", "autor_dominante", "fatia_dominante", "n_autores", "commits"] + (["autores_ativos"] if tem_tempo else [])
        st.dataframe(crit[cols_c], hide_index=True, width="stretch",
                     column_config={"arquivo": "Arquivo", "autor_dominante": "Autor dominante", "n_autores": "Autores", "commits": "Commits",
                                    "autores_ativos": "Autores ativos",
                                    "fatia_dominante": st.column_config.ProgressColumn("Fatia do autor dominante", format="%.0f%%",
                                                                                        min_value=0, max_value=100)})
        donos = mf[mf.critico].autor_dominante.value_counts()
        st.caption(f"Autores dominantes distintos entre os {n_crit} críticos: {len(donos)}. O mais frequente é {donos.index[0]} ({donos.iloc[0]}).")
    else:
        st.info("Nenhum arquivo é crítico com esses parâmetros. Reduza a concentração mínima na barra lateral.")

    st.markdown("#### Perda de cobertura: estratégias de remoção")
    legenda("Cada curva remove autores numa ordem diferente. <b>Aleatória</b> é a referência (saída natural de contribuidores). "
            "<b>Por grau</b> remove primeiro quem tocou mais arquivos. <b>Por betweenness</b> remove primeiro quem é ponte entre grupos "
            "na projeção autor–autor. <b>Gulosa</b> escolhe, a cada passo, quem deixaria mais arquivos órfãos naquele momento: é uma "
            "aproximação do pior caso. A linha pontilhada usa a definição ponderada: o arquivo perde o conhecimento quando quem "
            f"ficou soma menos de {int(an.LIMIAR_COBERTURA * 100)}% dos commits dele.")
    st.plotly_chart(figura_curvas(curva, R.get("tentativas_aleatorias", 200)), width="stretch", key="curva_remocao")
    if "gulosa_autores_para_50pct" in R:
        para = lambda v: "mais de 20% dos autores" if v is None else f"{v} autores"
        st.caption(f"Para deixar 50% dos arquivos órfãos: por grau, {para(R['autores_para_50pct_orfaos'])}; "
                   f"gulosa, {para(R['gulosa_autores_para_50pct'])}; por betweenness, {para(R['betweenness_autores_para_50pct'])}. "
                   f"Para perder o conhecimento de 50% dos arquivos (definição ponderada): {para(R['conhecimento_autores_para_50pct'])}.")

    with st.expander("Como os dados foram preparados"):
        st.markdown(
            f"- **Commits lidos** (sem merges): {milhar(R['funil_commits_lidos'])}\n"
            f"- **Registros commit–arquivo**: {milhar(R['funil_registros'])}\n"
            f"- **Registros de bots removidos**: {milhar(R['funil_registros_bots'])}\n"
            f"- **Nomes distintos → autores unificados** (por e-mail): {milhar(R['funil_nomes_distintos'])} → {milhar(R['funil_autores_unificados'])}\n"
            f"- **Registros descartados** (arquivo inexistente no HEAD, changelog, lockfiles): {milhar(R['funil_registros_filtrados_arquivo'])}\n"
            f"- **Rede final**: {milhar(n_a)} autores, {milhar(n_f)} arquivos, {milhar(n_e)} ligações")

# ----------------------------------------------------------------------------- 3. análises da rede
with tab_analises:
    st.subheader("O que a estrutura da rede revela")
    legenda("Cada bloco explica o que a medida significa <b>neste problema</b>: quem conhece o quê, quem liga quem e quem poderia assumir.")

    titulo("Distribuição de grau em escala log-log")
    legenda("Curva de P(K ≥ k): fração de nós com grau pelo menos k. Uma reta descendente longa indica <b>cauda pesada</b>: "
            "muitos autores com 1 arquivo e poucos hubs que tocam quase tudo. Quanto mais longa a cauda, mais a cobertura "
            "depende de poucas pessoas.")
    ccdf = []
    for nome, s in [("Arquivos por autor", ma.arquivos_tocados), ("Autores por arquivo", mf.n_autores)]:
        k = np.sort(s.unique())
        ccdf.append(pd.DataFrame({"k": k, "P(K ≥ k)": [(s >= x).mean() for x in k], "lado": nome}))
    fc = px.line(pd.concat(ccdf), x="k", y="P(K ≥ k)", color="lado", markers=True, log_x=True, log_y=True,
                 template="plotly_white", color_discrete_sequence=SERIES[:2])
    fc.update_traces(marker_size=8, line_width=2)
    fc.update_layout(height=360, legend_title_text="", xaxis_title="grau k", margin=dict(t=20))
    st.plotly_chart(fc, width="stretch", key="ccdf")

    if not tem_proj:
        precisa_reanalisar("a projeção autor–autor, a janela temporal e as demais análises novas")
    else:
        titulo("Projeção autor–autor e esqueleto por limiar")
        c = st.columns(4)
        c[0].metric("Pares de autores na projeção", milhar(R["proj_arestas_total"]),
                    help="MᵀM: dois autores ficam ligados se alteraram pelo menos um arquivo em comum.")
        c[1].metric(f"Pares com ≥ {R['esq_limiar']} arquivos em comum", milhar(R["esq_arestas"]))
        c[2].metric("Autores no esqueleto", f"{milhar(R['esq_autores'])} de {milhar(n_a)}")
        c[3].metric("Densidade do esqueleto", pct(R["esq_densidade_pct"], 2))
        legenda(f"A projeção completa liga todo mundo que tocou um arquivo popular, criando cliques artificiais (o <b>artefato de "
                f"projeção</b>). O esqueleto mantém só pares que dividem pelo menos {R['esq_limiar']} arquivos: a rede de quem "
                f"realmente trabalha junto. As medidas abaixo são calculadas nele.")

        titulo("Centralidades na projeção: quem é hub e quem é ponte")
        cols_p = ["autor", "arquivos_tocados", "grau_proj", "betweenness_proj", "eigenvector_proj", "closeness_proj",
                  "clustering_proj", "core_proj"] + (["ativo"] if tem_tempo else [])
        ordem_p = st.radio("Ordenar por", ["betweenness_proj", "eigenvector_proj", "closeness_proj", "grau_proj"], horizontal=True,
                           format_func={"betweenness_proj": "betweenness", "eigenvector_proj": "eigenvector",
                                        "closeness_proj": "closeness", "grau_proj": "grau na projeção"}.get)
        st.dataframe(ma.sort_values(ordem_p, ascending=False).head(15)[cols_p], hide_index=True, width="stretch",
                     column_config={"autor": "Autor", "arquivos_tocados": "Arquivos (bipartido)", "grau_proj": "Colegas (grau)",
                                    "betweenness_proj": st.column_config.NumberColumn("Betweenness", format="%.3f"),
                                    "eigenvector_proj": st.column_config.NumberColumn("Eigenvector", format="%.3f"),
                                    "closeness_proj": st.column_config.NumberColumn("Closeness", format="%.3f"),
                                    "clustering_proj": st.column_config.NumberColumn("Clustering", format="%.2f"),
                                    "core_proj": "Core", "ativo": "Ativo"})
        legenda("<b>Grau</b>: com quantos colegas a pessoa divide código. <b>Betweenness</b>: quantos caminhos mínimos entre colegas "
                "passam por ela; é quem liga grupos que, sem ela, não dividiriam código. <b>Eigenvector</b>: está ligada a outros "
                "bem ligados, ou seja, ao núcleo de decisão. <b>Closeness</b>: está a poucos passos de todos e consegue revisar qualquer "
                "parte. <b>Clustering</b> baixo num autor central indica uma <i>estrela</i>: os colegas dele não trabalham entre si.")
        rho = R.get("spearman_grau_btw_projecao")
        fs = px.scatter(ma[ma.grau_proj > 0], x="arquivos_tocados", y="betweenness_proj", color="nucleo", hover_name="autor",
                        log_x=True, template="plotly_white", color_discrete_map={True: SERIES[1], False: SERIES[0]},
                        labels={"arquivos_tocados": "arquivos tocados (grau no bipartido)", "betweenness_proj": "betweenness na projeção",
                                "nucleo": "no k-core máximo"})
        fs.update_traces(marker=dict(size=9, line=dict(width=1, color="white")))
        fs.update_layout(height=380, margin=dict(t=20))
        st.plotly_chart(fs, width="stretch", key="grau_btw")
        if rho is not None:
            st.caption(f"Correlação de Spearman entre grau e betweenness: {num(rho)}. Dos 10 maiores em grau, "
                       f"{R['top10_grau_e_btw_proj']} também estão entre os 10 maiores em betweenness. Correlação alta significa que "
                       "hubs e pontes são as mesmas pessoas e o risco se concentra nelas. Pontos acima da nuvem são "
                       "\"pontes silenciosas\": tocam poucos arquivos, mas ligam grupos.")

        titulo("Núcleo duro: k-core e k-truss")
        c = st.columns(4)
        c[0].metric("Degeneracy (k máximo)", R["proj_k_max"], help="Maior k tal que existe um subgrafo em que todo autor tem ≥ k colegas.")
        c[1].metric("Autores no k-core máximo", R["proj_nucleo_autores"])
        c[2].metric("k-truss máximo", R["proj_truss_max"], help="Toda ligação do subgrafo está em pelo menos k−2 triângulos.")
        c[3].metric("Autor principal no núcleo?", "sim" if R["principal_no_nucleo"] else "não")
        nucleo = ma[ma.nucleo]
        txt_ativos = f" Desses, {int(nucleo.ativo.sum())} continuam ativos." if tem_tempo else ""
        legenda(f"O k-core máximo é o grupo mínimo de pessoas que conhece o projeto de forma transversal: cada uma divide código "
                f"com pelo menos {R['proj_k_max']} outras do mesmo grupo. Ele tem <b>{R['proj_nucleo_autores']} autores</b>.{txt_ativos} "
                f"Se o núcleo é pequeno ou está majoritariamente inativo, o projeto depende de pouca gente, mesmo com centenas "
                f"de contribuidores.")
        st.dataframe(nucleo.sort_values("arquivos_tocados", ascending=False)[
            ["autor", "arquivos_tocados", "grau_proj"] + (["ultimo_commit", "ativo"] if tem_tempo else [])],
            hide_index=True, width="stretch",
            column_config={"autor": "Autor do núcleo", "arquivos_tocados": "Arquivos", "grau_proj": "Colegas",
                           "ultimo_commit": "Último commit", "ativo": "Ativo"})
        dist = ma[ma.grau_proj > 0].core_proj.value_counts().sort_index().reset_index()
        dist.columns = ["core", "autores"]
        fk = px.bar(dist, x="core", y="autores", template="plotly_white", color_discrete_sequence=[VERDE])
        fk.update_layout(height=300, xaxis_title="core number na projeção (camadas da cebola)", yaxis_title="autores", margin=dict(t=20))
        st.plotly_chart(fk, width="stretch", key="core_dist")

        titulo("Eficiência: quanto os caminhos se alongam sem essas pessoas")
        ef = pd.DataFrame({"cenário": ["rede completa", "sem o 1º em grau", "sem os 5 primeiros em grau",
                                       "sem o 1º em betweenness", "sem os 5 primeiros em betweenness"],
                           "eficiência": [R["efic_base"], R["efic_sem_top1_grau"], R["efic_sem_top5_grau"],
                                          R["efic_sem_top1_btw"], R["efic_sem_top5_btw"]]})
        ef["queda"] = (1 - ef["eficiência"] / R["efic_base"]) * 100
        st.dataframe(ef, hide_index=True, width="stretch",
                     column_config={"eficiência": st.column_config.NumberColumn("Eficiência global", format="%.3f"),
                                    "queda": st.column_config.NumberColumn("Queda", format="%.1f%%")})
        legenda("Eficiência global é a média de 1/distância entre pares de autores (estimada com 300 origens). Ela cai quando os "
                "caminhos se alongam e quando a rede se parte. Uma queda grande ao remover poucas pessoas significa que o "
                "conhecimento circula <b>através</b> delas: sem elas, quem sobra fica mais longe de quem sabe.")

        titulo("Sucessores: quem pode assumir os arquivos críticos")
        suc = sucessores_cache(escolhido, info["mtime"], min_commits, limiar)
        if suc.empty:
            st.info("Nenhum arquivo crítico com os parâmetros atuais.")
        else:
            st.dataframe(suc, hide_index=True, width="stretch",
                         column_config={"autor": "Autor dominante", "arquivos_criticos": "Críticos",
                                        "sucessor_jaccard": "Sucessor (maior Jaccard)",
                                        "jaccard": st.column_config.NumberColumn("Jaccard", format="%.2f"),
                                        "arquivos_em_comum": "Arquivos em comum",
                                        "cobertura_criticos_pct": st.column_config.NumberColumn("Críticos que o sucessor já tocou", format="%.0f%%"),
                                        "melhor_cobertura": "Quem mais cobre os críticos",
                                        "melhor_cobertura_pct": st.column_config.NumberColumn("Cobertura", format="%.0f%%"),
                                        "sucessor_ativo": "Sucessor ativo"})
            legenda("Jaccard = |arquivos em comum| / |arquivos de um ou de outro|. O maior Jaccard aponta quem tem o perfil mais "
                    "parecido no projeto todo. Mas parecido no geral não é o mesmo que conhecer os arquivos críticos: por isso a "
                    "tabela mostra também <b>quem mais cobre</b> esses arquivos. Quando as duas colunas discordam, ou a cobertura é "
                    "baixa, ou o sucessor está inativo, ninguém está pronto para assumir.")

        if tem_tempo:
            titulo("Janela temporal: quem ainda está no projeto")
            legenda(f"Cada autor recebe o atributo <i>ativo</i> se fez algum commit nos {R['janela_dias']} dias anteriores ao "
                    f"commit mais recente ({R['data_referencia']}). A rede inteira trata quem saiu há dez anos como disponível; "
                    f"esta visão mostra os arquivos que <b>já</b> estão órfãos hoje, sem simular nenhuma saída.")
            sem = mf[mf.autores_ativos == 0].sort_values("commits", ascending=False)
            c = st.columns(3)
            c[0].metric("Autores ativos", f"{R['n_autores_ativos']} de {milhar(n_a)}")
            c[1].metric("Arquivos sem autor ativo", f"{len(sem)} ({pct(len(sem) / n_f * 100)})")
            c[2].metric("Críticos com dono inativo", int((mf.critico & ~mf.dono_ativo).sum()))
            if len(sem):
                st.dataframe(sem.head(50)[["arquivo", "autor_dominante", "n_autores", "commits"]], hide_index=True, width="stretch",
                             column_config={"arquivo": "Arquivo sem ninguém ativo", "autor_dominante": "Autor dominante",
                                            "n_autores": "Autores (histórico)", "commits": "Commits"})

        titulo("Subredes por pasta: onde o risco mora")
        pastas = d.get("pastas")
        if pastas is not None and len(pastas):
            st.dataframe(pastas, hide_index=True, width="stretch",
                         column_config={"pasta": "Pasta", "arquivos": "Arquivos", "autores": "Autores", "ligacoes": "Ligações",
                                        "densidade_pct": st.column_config.NumberColumn("Densidade", format="%.1f%%"),
                                        "arquivos_1_autor_pct": st.column_config.NumberColumn("Autor único", format="%.0f%%"),
                                        "criticos": "Críticos",
                                        "criticos_pct": st.column_config.NumberColumn("Críticos (% eleg.)", format="%.0f%%"),
                                        "autor_principal": "Principal da pasta",
                                        "arquivos_do_principal_pct": st.column_config.NumberColumn("Arquivos dele", format="%.0f%%"),
                                        "orfaos_sem_principal_pct": st.column_config.NumberColumn("Órfãos sem ele", format="%.0f%%"),
                                        "sem_autor_ativo_pct": st.column_config.NumberColumn("Sem autor ativo", format="%.0f%%")})
            legenda("Cada linha é a subrede induzida pelos arquivos de uma pasta (críticos com os parâmetros padrão, 80% e 3 commits). "
                    "Concentração em <code>docs</code> ou <code>examples</code> preocupa pouco; concentração no código-fonte é o achado "
                    "principal.")

        titulo("Sensibilidade da definição de arquivo crítico")
        sens = d.get("sensibilidade")
        if sens is not None:
            pv = sens.pivot(index="min_commits", columns="limiar_pct", values="criticos_pct")
            fh = px.imshow(pv, text_auto=".0f", color_continuous_scale="Greens", aspect="auto",
                           labels=dict(x="concentração mínima do autor dominante (%)", y="mínimo de commits", color="% críticos"))
            fh.update_layout(height=320, margin=dict(t=20))
            st.plotly_chart(fh, width="stretch", key="sensibilidade")
            dn = sens.pivot(index="min_commits", columns="limiar_pct", values="donos_distintos")
            legenda("O número de arquivos críticos muda bastante com os limiares (é uma escolha). O que importa é se a conclusão muda: "
                    f"o número de donos distintos varia entre <b>{int(dn.values.min())}</b> e <b>{int(dn.values.max())}</b> nas 16 "
                    f"combinações, e o dono mais frequente é sempre "
                    f"<b>{sens.dono_mais_frequente.mode().iloc[0]}</b>" +
                    (" em todas." if sens.dono_mais_frequente.nunique() == 1 else " na maioria delas."))

        titulo("Efeito da amostragem: ler só os commits mais recentes")
        am = d.get("amostragem")
        if am is not None and len(am) > 1:
            met = {"arquivos_1_autor_pct": "arquivos de autor único", "orfaos_sem_principal_pct": "órfãos sem o autor principal",
                   "arquivos_do_principal_pct": "arquivos do autor principal", "autores_1_arquivo_pct": "autores com 1 arquivo"}
            lo = am.melt("commits", list(met), var_name="medida", value_name="%")
            lo["medida"] = lo.medida.map(met)
            fa = px.line(lo, x="commits", y="%", color="medida", markers=True, log_x=True, template="plotly_white",
                         color_discrete_sequence=SERIES[:4])
            fa.update_traces(marker_size=8, line_width=2)
            fa.update_layout(height=360, legend_title_text="", xaxis_title="commits mais recentes usados na rede (escala log)",
                             yaxis_title="%", margin=dict(t=20))
            st.plotly_chart(fa, width="stretch", key="amostragem")
            st.dataframe(am, hide_index=True, width="stretch",
                         column_config={c: st.column_config.NumberColumn(format="%.1f") for c in am.columns if c.endswith("_pct")})
            legenda("Limitar o histórico é uma amostragem enviesada: ela vê só quem está ativo agora e só os arquivos tocados "
                    "recentemente. Se as curvas mudam muito entre o recorte e o histórico completo, uma análise feita com "
                    "<code>--max-commits</code> descreve o momento atual, não a história do projeto. Os tamanhos contam commits que "
                    "tocam arquivos atuais.")
        else:
            st.info("O histórico é pequeno demais para comparar recortes.")

# ----------------------------------------------------------------------------- 4. simulação
with tab_sim:
    st.subheader("Simule a saída de autores")
    legenda("Escolha quem sai do projeto. Um arquivo fica <b>órfão</b> quando todos os autores que o alteraram foram "
            "removidos. A lista está ordenada por número de arquivos tocados.")
    ordem_autores = ma.sort_values(["arquivos_tocados", "commits"], ascending=False).autor.tolist()
    graus = ma.set_index("autor").arquivos_tocados.to_dict()
    escolhidos = st.multiselect("Autores removidos", ordem_autores, default=[], format_func=lambda a: f"{a} · {graus[a]} arquivos",
                                placeholder="Digite um nome ou escolha na lista")
    n_top = st.slider("Ou remover os N autores mais conectados", 0, min(50, n_a - 1), 0)
    inativos = []
    if tem_tempo:
        if st.checkbox(f"Remover também todos os autores inativos (sem commits nos últimos {R['janela_dias']} dias)",
                       help="Mostra a situação de hoje: só quem continua ativo conta como cobertura."):
            inativos = ma.loc[~ma.ativo, "autor"].tolist()
    removidos = list(dict.fromkeys(ordem_autores[:n_top] + escolhidos + inativos))

    orfaos, gcc, n_comp = simular(G, removidos)
    perdidos = perda_conhecimento(d["arestas"], removidos)
    pct_aut, pct_orf = len(removidos) / n_a * 100, len(orfaos) / n_f * 100
    base = float(np.interp(pct_aut, curva.pct_autores_removidos, curva.orfaos_aleatoria_pct)) \
        if pct_aut <= curva.pct_autores_removidos.max() else None
    perdeu_dono = mf[mf.critico & mf.autor_dominante.isin(removidos)]

    c = st.columns(5)
    c[0].metric("Autores removidos", f"{len(removidos)} ({pct(pct_aut, 2)})")
    c[1].metric("Arquivos órfãos", f"{len(orfaos)} ({pct(pct_orf)})", help="Arquivos sem nenhum autor restante.")
    c[2].metric("Perda de conhecimento", f"{len(perdidos)} ({pct(len(perdidos) / n_f * 100)})",
                help=f"Arquivos em que quem ficou soma menos de {int(an.LIMIAR_COBERTURA * 100)}% dos commits. Inclui os órfãos.")
    c[3].metric("Críticos sem o autor dominante", f"{len(perdeu_dono)} de {n_crit}")
    c[4].metric("Maior componente da rede", pct(gcc * 100) if removidos else "100,0%",
                help=f"Fatia dos nós restantes no maior componente. Componentes: {n_comp if removidos else 1}.")
    if removidos:
        if base is not None:
            st.info(f"Para comparação, remover o mesmo percentual de autores **ao acaso** deixaria, em média, **{pct(base, 2)}** dos "
                    f"arquivos órfãos. Esta escolha deixou **{pct(pct_orf, 2)}**.")
        else:
            st.info("A referência aleatória foi calculada até 20% dos autores removidos.")
    else:
        st.caption("Nenhum autor selecionado. Comece pelos primeiros da lista, que são os mais conectados.")

    st.plotly_chart(figura_grafo(visual, pos, ma_d, mf_d, frozenset(removidos), frozenset(orfaos)), width="stretch",
                    config={"scrollZoom": True, "displaylogo": False}, key="grafo_simulacao")
    if visual is not G:
        st.caption("O desenho mostra só parte da rede; as métricas acima usam a rede completa.")
    if orfaos:
        st.markdown("#### Arquivos que ficaram sem autor")
        tabela = mf[mf.arquivo.isin(orfaos)].sort_values("commits", ascending=False)[["arquivo", "autor_dominante", "n_autores", "commits"]]
        st.dataframe(tabela.head(200), hide_index=True, width="stretch",
                     column_config={"arquivo": "Arquivo", "autor_dominante": "Último dono (maior fatia)",
                                    "n_autores": "Autores antes", "commits": "Commits"})
        if len(tabela) > 200:
            st.caption(f"Mostrando 200 de {len(tabela)} arquivos, os de mais commits.")

# ----------------------------------------------------------------------------- 5. comparação
with tab_comp:
    st.subheader("Compare os projetos analisados")
    legenda("Quanto mais íngreme a curva e maior o percentual de órfãos ao remover o autor principal, mais "
            "concentrado está o conhecimento do projeto. Os arquivos críticos usam os parâmetros da barra lateral.")
    if len(projetos) < 2:
        st.info("Analise pelo menos dois projetos para comparar.")
    else:
        linhas, curvas = [], []
        para = lambda v: "> 20% dos autores" if v is None else str(v)
        for p in projetos:
            dp = carregar_projeto(p["slug"], p["mtime"]); rp = dp["resumo"]
            mfp = dp["mf"].copy(); mfp["critico"] = an.marcar_criticos(mfp, min_commits, limiar)
            el = int((mfp.commits >= min_commits).sum())
            nome = p["repo"]
            linhas.append({"Projeto": nome, "Commits lidos": milhar(rp["funil_commits_lidos"]), "Autores": milhar(rp["n_autores"]),
                           "Arquivos": milhar(rp["n_arquivos"]), "Autor principal": rp["autor_principal"],
                           "Arquivos do autor principal": pct(rp["pct_arquivos_autor_principal"]),
                           "Arquivos de autor único": pct(rp["pct_arquivos_1_autor"]),
                           "Críticos (% dos elegíveis)": pct(mfp.critico.sum() / el * 100) if el else "—",
                           "Órfãos sem o autor principal": pct(rp["orfaos_1_autor_pct"]),
                           "Órfãos sem 1 autor ao acaso": pct(rp["orfaos_1_autor_aleatoria_pct"], 2),
                           "Autores p/ 50% de órfãos (grau)": para(rp["autores_para_50pct_orfaos"]),
                           "Autores p/ 50% de órfãos (gulosa)": para(rp.get("gulosa_autores_para_50pct", "—")),
                           "Núcleo (k-core máx.)": str(rp.get("proj_nucleo_autores", "—")),
                           "Autores ativos": milhar(rp["n_autores_ativos"]) if "n_autores_ativos" in rp else "—",
                           "Arquivos sem autor ativo": pct(rp["pct_arquivos_sem_autor_ativo"]) if "pct_arquivos_sem_autor_ativo" in rp else "—"})
            c = dp["curva"][["pct_autores_removidos", "orfaos_direcionada_pct"]].copy(); c["Projeto"] = nome; curvas.append(c)
        st.dataframe(pd.DataFrame(linhas), hide_index=True, width="stretch")
        f4 = px.line(pd.concat(curvas), x="pct_autores_removidos", y="orfaos_direcionada_pct", color="Projeto", template="plotly_white",
                     title="Arquivos órfãos na remoção direcionada", color_discrete_sequence=SERIES)
        f4.update_traces(line_width=2)
        f4.update_layout(xaxis_title="% de autores removidos", yaxis_title="% de arquivos órfãos", height=400)
        st.plotly_chart(f4, width="stretch", key="curva_comparacao")
        st.caption("Cada projeto tem tamanho diferente, então a comparação é por porcentagem de autores. "
                   "Projetos analisados com limite de commits descrevem só o recorte recente.")
