"""Núcleo da análise: coleta o histórico Git de um repositório do GitHub e calcula as métricas
da rede bipartida autor–arquivo e da sua projeção autor–autor. É a mesma lógica do notebook,
empacotada para ser usada pela aplicação (app.py), pelo notebook e por linha de comando:

    python analise.py pallets/flask
    python analise.py https://github.com/psf/requests --max-commits 5000

Mapa das funções para o conteúdo do curso (detalhado no README):
    analisar()              grafo bipartido, grau, densidade, componentes, betweenness, k-core   (sem. 2, 4, 5, 6)
    matriz_incidencia()     matriz de incidência arquivo × autor                                (sem. 2 e 6)
    projecao_autores()      projeção MᵀM, Jaccard e esqueleto por limiar                         (sem. 6)
    metricas_projecao()     centralidades, clustering, k-core, k-truss, eficiência               (sem. 4 e 5)
    curvas_remocao()        remoção aleatória, por grau, por betweenness e gulosa                 (sem. 4)
    sucessores()            sucessor de cada autor crítico por Jaccard                            (sem. 6)
    metricas_temporais()    janela temporal: autores ativos e arquivos sem ninguém ativo          (sem. 2, atributos)
    sensibilidade()         efeito dos limiares do "arquivo crítico"                              (senso crítico)
    subredes_por_pasta()    subredes induzidas por diretório                                      (sem. 2)
    efeito_amostragem()     quanto as métricas mudam ao ler só os N commits mais recentes          (sem. 3)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd
from networkx.algorithms import bipartite
from scipy import sparse

SEED = 42
N_TRIALS = 200
MIN_COMMITS_CRITICO = 3
LIMIAR_CRITICO = 0.80
MIN_AUTORES, MIN_ARQUIVOS = 5, 5
MIN_ARQ_COMUNS = 2          # esqueleto da projeção: dois autores ficam ligados se dividem >= 2 arquivos
LIMIAR_COBERTURA = 0.20     # "perda de conhecimento": quem sobrou tem < 20% dos commits do arquivo
JANELA_DIAS = 730           # autor ativo = fez algum commit nos últimos 2 anos do histórico
AMOSTRAS_COMMITS = (250, 500, 1000, 2000, 5000, 10000, 20000)
FONTES_AMOSTRA = 300        # origens usadas na betweenness e na eficiência aproximadas

BOT = re.compile(r"\[bot\]|dependabot|pre-commit-ci|github-actions|renovate|imgbot|(?:^|[-_ ])bot$", re.I)
RUIDO = re.compile(r"(?:^|/)(?:CHANGES\.rst|CHANGELOG.*|.*\.lock|requirements/.*\.txt|package-lock\.json|pnpm-lock\.yaml|go\.sum)$")

PASTA_PROJETOS = Path(__file__).parent / "projetos"
PROJETO_INICIAL = "pallets/flask"   # aparece primeiro na lista
_URL = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?(?:[/#?].*)?$")
_CURTO = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$")


class ErroAnalise(Exception):
    """Erro esperado, com mensagem pensada para ser mostrada ao usuário."""


# ----------------------------------------------------------------------------- entrada
def normalizar_entrada(texto: str) -> tuple[str, str, str]:
    """Aceita 'dono/repo' ou um link do GitHub e devolve (dono, repo, url https)."""
    t = (texto or "").strip()
    m = _URL.match(t) or _CURTO.match(t)
    if not m:
        raise ErroAnalise("Não reconheci esse endereço. Use um link do GitHub (https://github.com/dono/repositório) "
                          "ou o formato dono/repositório.")
    dono, repo = m.group(1), m.group(2)
    if dono in (".", "..") or repo in (".", ".."):
        raise ErroAnalise("Endereço inválido.")
    return dono, repo, f"https://github.com/{dono}/{repo}.git"


def slug(dono: str, repo: str) -> str:
    return f"{dono}__{repo}".lower()


# ----------------------------------------------------------------------------- git
def _git(args: list[str], cwd: Path | None = None, timeout: int = 1800) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo", "LC_ALL": "C.UTF-8"}
    cmd = ["git", "-c", "core.quotepath=false", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", *args]
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise ErroAnalise("O tempo limite foi atingido. Tente reduzir o número de commits analisados.")
    except FileNotFoundError:
        raise ErroAnalise("O Git não está instalado neste computador.")
    if p.returncode != 0:
        msg = (p.stderr or "").strip().splitlines()
        ultima = msg[-1] if msg else "erro desconhecido"
        if "not found" in p.stderr.lower() or "could not read" in p.stderr.lower() or "authentication" in p.stderr.lower():
            raise ErroAnalise("Repositório não encontrado, ou é privado. Só repositórios públicos do GitHub são aceitos.")
        raise ErroAnalise(f"Falha ao executar o Git: {ultima}")
    return p.stdout


def ler_log(bruto: str) -> pd.DataFrame:
    """Converte a saída de `git log --name-only --pretty=format:@@%H%x1f%an%x1f%ae%x1f%at` em registros commit–arquivo."""
    linhas, cur = [], None
    for ln in bruto.splitlines():
        if ln.startswith("@@"):
            partes = ln[2:].split("\x1f")
            if len(partes) != 4:
                cur = None
                continue
            h, nome, email, ts = partes
            cur = (h, nome.strip(), email.strip().lower(), int(ts) if ts.isdigit() else 0)
        elif ln.strip() and cur:
            linhas.append((*cur, ln.strip()))
    return pd.DataFrame(linhas, columns=["commit", "nome", "email", "ts", "arquivo"])


def coletar_historico(url: str, max_commits: int | None = None, progress=None) -> tuple[pd.DataFrame, set, dict]:
    """Clona (sem arquivos, só metadados), lê o histórico e apaga o clone. Retorna (registros, arquivos do HEAD, meta)."""
    p = progress or (lambda f, m: None)
    tmp = Path(tempfile.mkdtemp(prefix="aed2_"))
    try:
        dest = tmp / "repo"
        p(0.05, "Baixando o histórico do repositório…")
        _git(["clone", "--quiet", "--filter=blob:none", "--no-checkout", url, str(dest)], timeout=1200)
        head = _git(["rev-parse", "HEAD"], cwd=dest).strip()
        total = int(_git(["rev-list", "--no-merges", "--count", "HEAD"], cwd=dest).strip() or 0)
        if total == 0:
            raise ErroAnalise("O repositório não tem commits (ou só tem merges).")
        p(0.25, f"Lendo os commits ({min(total, max_commits) if max_commits else total:,} de {total:,})…".replace(",", "."))
        args = ["log", "--no-merges", "--name-only", "--pretty=format:@@%H%x1f%an%x1f%ae%x1f%at"]
        if max_commits:
            args.append(f"--max-count={int(max_commits)}")
        bruto = _git(args, cwd=dest)
        arquivos_head = set(f for f in _git(["ls-tree", "-r", "--name-only", "-z", "HEAD"], cwd=dest).split("\0") if f)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    p(0.4, "Organizando os registros…")
    meta = {"head_sha": head, "commits_sem_merges_total": total, "limite_commits": max_commits}
    return ler_log(bruto), arquivos_head, meta


def limpar(df: pd.DataFrame, arquivos_head: set) -> tuple[pd.DataFrame, dict, pd.DataFrame]:
    """Remove bots, unifica identidades e mantém só arquivos existentes no HEAD e sem ruído.

    Retorna (arestas autor–arquivo com peso e data do último commit, funil de limpeza, registros commit–arquivo limpos).
    """
    funil = {"registros": int(len(df)), "commits_lidos": int(df.commit.nunique())}
    antes = len(df)
    df = df[~(df.nome.str.contains(BOT) | df.email.str.contains(BOT))].copy()
    funil["registros_bots"] = int(antes - len(df))
    if df.empty:
        raise ErroAnalise("Só encontrei commits de bots neste repositório.")
    funil["nomes_distintos"] = int(df.nome.nunique())
    nome_do_email = df.groupby("email").nome.agg(lambda s: s.value_counts().idxmax())
    df["autor"] = df.email.map(nome_do_email)
    funil["autores_unificados"] = int(df.autor.nunique())
    antes = len(df)
    df = df[df.arquivo.isin(arquivos_head) & ~df.arquivo.str.contains(RUIDO)]
    funil["registros_filtrados_arquivo"] = int(antes - len(df))
    arestas = (df.groupby(["autor", "arquivo"])
                 .agg(commits=("commit", "nunique"), ultimo_ts=("ts", "max")).reset_index())
    return arestas, funil, df[["commit", "ts", "autor", "arquivo"]].reset_index(drop=True)


# ----------------------------------------------------------------------------- estruturas
def matriz_incidencia(arestas: pd.DataFrame, arquivos: list, autores: list, peso: str | None = None) -> sparse.csr_matrix:
    """Matriz de incidência arquivo × autor (semanas 2 e 6). Binária, ou com o valor da coluna `peso`."""
    idx_f = {f: i for i, f in enumerate(arquivos)}
    idx_a = {a: j for j, a in enumerate(autores)}
    vals = arestas[peso].to_numpy(float) if peso else np.ones(len(arestas))
    return sparse.csr_matrix((vals, ([idx_f[f] for f in arestas.arquivo], [idx_a[a] for a in arestas.autor])),
                             shape=(len(arquivos), len(autores)))


def projecao_autores(M: sparse.csr_matrix, autores: list, min_comum: int = MIN_ARQ_COMUNS) -> tuple[nx.Graph, dict]:
    """Projeção do bipartido sobre os autores (semana 6).

    `MᵀM` dá, para cada par de autores, quantos arquivos os dois alteraram. O peso da aresta é esse número e o
    atributo `jaccard` é |A∩B| / |A∪B|. O esqueleto por limiar descarta pares com menos de `min_comum` arquivos em
    comum, porque um arquivo muito popular ligaria centenas de contribuidores ocasionais em um clique artificial.
    """
    grau = np.asarray(M.sum(0)).ravel()
    C = (M.T @ M).tocoo()
    mk = C.row < C.col
    i, j, w = C.row[mk], C.col[mk], C.data[mk]
    jac = w / (grau[i] + grau[j] - w)
    keep = w >= min_comum
    S = nx.Graph()
    S.add_edges_from((autores[a], autores[b], {"weight": float(x), "jaccard": float(y)})
                     for a, b, x, y in zip(i[keep], j[keep], w[keep], jac[keep]))
    n_a = len(autores)
    info = {"proj_arestas_total": int(len(w)), "proj_densidade_total_pct": len(w) / (n_a * (n_a - 1) / 2) * 100 if n_a > 1 else 0.0,
            "esq_limiar": int(min_comum), "esq_autores": S.number_of_nodes(), "esq_arestas": S.number_of_edges(),
            "esq_densidade_pct": nx.density(S) * 100 if S.number_of_nodes() > 1 else 0.0}
    return S, info


def eficiencia(G: nx.Graph, fontes: list) -> float:
    """Eficiência global (semana 4) estimada a partir de um conjunto de origens: média de 1/d(s, t).

    Pares desconectados contribuem com 0, então a medida cai tanto quando caminhos se alongam quanto quando a rede se parte.
    """
    fontes = [s for s in fontes if s in G]
    n = G.number_of_nodes()
    if n < 2 or not fontes:
        return 0.0
    tot = 0.0
    for s in fontes:
        tot += sum(1.0 / d for d in nx.single_source_shortest_path_length(G, s).values() if d)
    return tot / (len(fontes) * (n - 1))


def truss_max(G: nx.Graph) -> tuple[int, nx.Graph]:
    """Maior k com k-truss não vazio (semana 5): toda aresta do subgrafo está em pelo menos k−2 triângulos."""
    k, H = 2, G
    while True:
        H2 = nx.k_truss(H, k + 1)
        if H2.number_of_edges() == 0:
            return k, H
        k, H = k + 1, H2


def metricas_projecao(S: nx.Graph, autores: list) -> tuple[pd.DataFrame, dict, nx.Graph]:
    """Centralidades, clustering, k-core e k-truss na projeção autor–autor (semanas 4 e 5)."""
    cols = ["grau_proj", "betweenness_proj", "eigenvector_proj", "closeness_proj", "clustering_proj", "core_proj", "nucleo"]
    if S.number_of_edges() == 0:
        return pd.DataFrame(index=autores, columns=cols).fillna(0), {"esq_vazio": True}, S
    gcc = S.subgraph(max(nx.connected_components(S), key=len)).copy()
    btw = nx.betweenness_centrality(S, k=min(FONTES_AMOSTRA, S.number_of_nodes()), seed=SEED, normalized=True)
    try:
        eig = nx.eigenvector_centrality_numpy(gcc, weight="weight")
    except Exception:  # grafos minúsculos ou degenerados
        eig = {}
    clo = nx.closeness_centrality(gcc) if gcc.number_of_nodes() <= 4000 else {}
    clu = nx.clustering(S)
    core = nx.core_number(S)
    kmax = max(core.values())
    df = pd.DataFrame({
        "grau_proj": pd.Series(dict(S.degree())), "betweenness_proj": pd.Series(btw), "eigenvector_proj": pd.Series(eig, dtype=float),
        "closeness_proj": pd.Series(clo, dtype=float), "clustering_proj": pd.Series(clu), "core_proj": pd.Series(core)},
    ).reindex(autores)
    df[["grau_proj", "betweenness_proj", "eigenvector_proj", "closeness_proj", "clustering_proj", "core_proj"]] = \
        df[["grau_proj", "betweenness_proj", "eigenvector_proj", "closeness_proj", "clustering_proj", "core_proj"]].fillna(0)
    df["nucleo"] = df.core_proj == kmax
    tk, T = truss_max(gcc)
    info = {"esq_componentes": nx.number_connected_components(S), "esq_gcc_pct": gcc.number_of_nodes() / S.number_of_nodes() * 100,
            "esq_clustering_medio": float(nx.average_clustering(S)), "esq_transitividade": float(nx.transitivity(S)),
            "proj_k_max": int(kmax), "proj_nucleo_autores": int(df.nucleo.sum()),
            "proj_truss_max": int(tk), "proj_truss_autores": int(T.number_of_nodes())}
    return df, info, gcc


def _curva_orfaos(Mc, grau_ini, ordem, n_f):
    """Fração de arquivos sem nenhum autor restante após remover os autores de `ordem`, um a um."""
    rem = grau_ini.copy(); out = [0.0]; orf = 0
    for j in ordem:
        idx = Mc.indices[Mc.indptr[j]:Mc.indptr[j + 1]]
        rem[idx] -= 1
        orf += int((rem[idx] == 0).sum())
        out.append(orf / n_f)
    return np.array(out)


def _curva_conhecimento(Wc, tot, ordem, n_f, limiar=LIMIAR_COBERTURA):
    """Versão ponderada: o arquivo perde o conhecimento quando quem sobrou tem menos de `limiar` dos seus commits."""
    rem = tot.astype(float).copy(); perdido = np.zeros(len(tot), bool); out = [0.0]
    for j in ordem:
        sl = slice(Wc.indptr[j], Wc.indptr[j + 1])
        idx = Wc.indices[sl]
        rem[idx] -= Wc.data[sl]
        perdido[idx] |= rem[idx] < limiar * tot[idx] - 1e-9
        out.append(perdido.sum() / n_f)
    return np.array(out)


def _ordem_gulosa(M, Mc, grau_ini, K):
    """Remoção gulosa adaptativa: a cada passo sai quem deixaria mais arquivos órfãos agora (recalculado a cada passo)."""
    MT = M.T.tocsr()
    grau_a = np.asarray(M.sum(0)).ravel()
    rem = grau_ini.copy(); livre = np.ones(M.shape[1], bool); ordem = []
    for _ in range(K):
        s = MT @ (rem == 1).astype(float) + 1e-6 * grau_a   # desempate pelo grau
        s[~livre] = -1
        j = int(np.argmax(s))
        ordem.append(j); livre[j] = False
        rem[Mc.indices[Mc.indptr[j]:Mc.indptr[j + 1]]] -= 1
    return np.array(ordem, dtype=int)


def curvas_remocao(M, W, ordens: dict, K: int, rng: np.random.Generator) -> tuple[pd.DataFrame, int, np.ndarray]:
    """Curvas de arquivos órfãos para várias estratégias de remoção de autores (robustez, semana 4)."""
    n_f, n_a = M.shape
    Mc, Wc = M.tocsc(), W.tocsc()
    grau_ini = np.asarray(M.sum(1)).ravel()
    tot = np.asarray(W.sum(1)).ravel()
    ordens = {**ordens, "gulosa": _ordem_gulosa(M, Mc, grau_ini, K)}
    n_trials = int(min(N_TRIALS, max(30, 400_000 // K)))
    alea = np.mean([_curva_orfaos(Mc, grau_ini, rng.permutation(n_a)[:K], n_f) for _ in range(n_trials)], axis=0)
    curva = pd.DataFrame({"pct_autores_removidos": np.arange(K + 1) / n_a * 100,
                          "orfaos_direcionada_pct": _curva_orfaos(Mc, grau_ini, ordens["grau"][:K], n_f) * 100,
                          "orfaos_aleatoria_pct": alea * 100,
                          "orfaos_betweenness_pct": _curva_orfaos(Mc, grau_ini, ordens["betweenness"][:K], n_f) * 100,
                          "orfaos_gulosa_pct": _curva_orfaos(Mc, grau_ini, ordens["gulosa"][:K], n_f) * 100,
                          "conhecimento_direcionada_pct": _curva_conhecimento(Wc, tot, ordens["grau"][:K], n_f) * 100})
    return curva, n_trials, ordens["gulosa"]


# ----------------------------------------------------------------------------- análises de apoio
def marcar_criticos(mf: pd.DataFrame, minimo: int = MIN_COMMITS_CRITICO, limiar: float = LIMIAR_CRITICO) -> pd.Series:
    return (mf.commits >= minimo) & (mf.fatia_dominante >= limiar)


def sucessores(arestas: pd.DataFrame, mf: pd.DataFrame, ma: pd.DataFrame | None = None, top: int = 20) -> pd.DataFrame:
    """Para cada autor dominante de arquivos críticos, quem mais se parece com ele (Jaccard, semana 6).

    `mf` precisa das colunas arquivo, critico e autor_dominante. Também mede a cobertura: que fração dos arquivos
    críticos do autor o sucessor já alterou, e quem teria a maior cobertura (que nem sempre é o de maior Jaccard).
    """
    crit = mf[mf.critico]
    if crit.empty:
        return pd.DataFrame(columns=["autor", "arquivos_criticos", "sucessor_jaccard", "jaccard", "arquivos_em_comum",
                                     "cobertura_criticos_pct", "melhor_cobertura", "melhor_cobertura_pct", "sucessor_ativo"])
    autores = sorted(arestas.autor.unique()); arquivos = sorted(arestas.arquivo.unique())
    idx_a = {a: j for j, a in enumerate(autores)}; idx_f = {f: i for i, f in enumerate(arquivos)}
    M = matriz_incidencia(arestas, arquivos, autores)
    Mc = M.tocsc()
    grau = np.asarray(M.sum(0)).ravel()
    ativo = ma.set_index("autor").ativo.to_dict() if ma is not None and "ativo" in ma else {}
    linhas = []
    for autor, n in crit.autor_dominante.value_counts().head(top).items():
        j = idx_a[autor]
        meus = Mc.indices[Mc.indptr[j]:Mc.indptr[j + 1]]
        inter = np.asarray(M[meus].sum(0)).ravel(); inter[j] = 0
        jac = inter / (grau + grau[j] - inter)
        s = int(np.argmax(jac))
        linhas_crit = [idx_f[f] for f in crit[crit.autor_dominante == autor].arquivo]
        cob = np.asarray(M[linhas_crit].sum(0)).ravel(); cob[j] = 0
        b = int(np.argmax(cob))
        linhas.append({"autor": autor, "arquivos_criticos": int(n),
                       "sucessor_jaccard": autores[s] if jac[s] > 0 else None, "jaccard": float(jac[s]),
                       "arquivos_em_comum": int(inter[s]), "cobertura_criticos_pct": float(cob[s] / n * 100),
                       "melhor_cobertura": autores[b] if cob[b] > 0 else None, "melhor_cobertura_pct": float(cob[b] / n * 100),
                       "sucessor_ativo": ativo.get(autores[s])})
    return pd.DataFrame(linhas)


def metricas_temporais(arestas: pd.DataFrame, janela_dias: int = JANELA_DIAS) -> tuple[pd.Series, pd.Series, dict]:
    """Janela temporal: um autor é ativo se fez algum commit nos últimos `janela_dias` do histórico analisado.

    Retorna (ativo por autor, autores ativos por arquivo, resumo). A referência é o commit mais recente, não a data de hoje,
    para que repositórios arquivados não apareçam como 100% inativos.
    """
    ref = int(arestas.ultimo_ts.max())
    corte = ref - janela_dias * 86400
    ult = arestas.groupby("autor").ultimo_ts.max()
    ativo = ult >= corte
    ativos_arq = arestas[arestas.autor.map(ativo)].groupby("arquivo").autor.nunique()
    info = {"janela_dias": janela_dias, "data_referencia": dt.datetime.fromtimestamp(ref, dt.timezone.utc).date().isoformat(),
            "n_autores_ativos": int(ativo.sum())}
    return ativo, ativos_arq, info


def sensibilidade(mf: pd.DataFrame, limiares=(0.6, 0.7, 0.8, 0.9), minimos=(1, 3, 5, 10)) -> pd.DataFrame:
    """Quantos arquivos são críticos (e quantos donos distintos há) para cada combinação de parâmetros."""
    linhas = []
    for mn in minimos:
        el = int((mf.commits >= mn).sum())
        for lim in limiares:
            c = marcar_criticos(mf, mn, lim)
            donos = mf[c].autor_dominante.value_counts()
            linhas.append({"min_commits": mn, "limiar_pct": int(lim * 100), "elegiveis": el, "criticos": int(c.sum()),
                           "criticos_pct": c.sum() / el * 100 if el else 0.0, "donos_distintos": int(len(donos)),
                           "dono_mais_frequente": donos.index[0] if len(donos) else None,
                           "fatia_do_mais_frequente_pct": donos.iloc[0] / c.sum() * 100 if len(donos) else 0.0})
    return pd.DataFrame(linhas)


def pasta_de(arquivo: str) -> str:
    """Diretório usado para as subredes: o primeiro nível, ou os dois primeiros em src/, lib/, pkg/ e packages/."""
    p = arquivo.split("/")
    if len(p) == 1:
        return "(raiz)"
    if p[0] in ("src", "lib", "pkg", "packages") and len(p) > 2:
        return "/".join(p[:2])
    return p[0]


def subredes_por_pasta(arestas: pd.DataFrame, mf: pd.DataFrame, min_arquivos: int = 5) -> pd.DataFrame:
    """Subrede induzida por diretório (semana 2): onde, no projeto, a dependência de poucas pessoas é maior."""
    ar = arestas.assign(pasta=arestas.arquivo.map(pasta_de))
    mfp = mf.assign(pasta=mf.arquivo.map(pasta_de))
    linhas = []
    for pasta, g in ar.groupby("pasta"):
        f = mfp[mfp.pasta == pasta]
        if len(f) < min_arquivos:
            continue
        por_autor = g.groupby("autor").arquivo.nunique()
        dono = por_autor.idxmax()
        unicos = set(f[f.n_autores == 1].arquivo)
        so_dono = g[g.arquivo.isin(unicos) & (g.autor == dono)].arquivo.nunique()
        el = int((f.commits >= MIN_COMMITS_CRITICO).sum())
        lin = {"pasta": pasta, "arquivos": len(f), "autores": int(g.autor.nunique()), "ligacoes": len(g),
               "densidade_pct": len(g) / (g.autor.nunique() * len(f)) * 100,
               "arquivos_1_autor_pct": (f.n_autores == 1).mean() * 100,
               "criticos": int(f.critico.sum()), "criticos_pct": f.critico.sum() / el * 100 if el else 0.0,
               "autor_principal": dono, "arquivos_do_principal_pct": por_autor.max() / len(f) * 100,
               "orfaos_sem_principal_pct": so_dono / len(f) * 100}
        if "autores_ativos" in f:
            lin["sem_autor_ativo_pct"] = (f.autores_ativos == 0).mean() * 100
        linhas.append(lin)
    return pd.DataFrame(linhas).sort_values("arquivos", ascending=False).reset_index(drop=True) if linhas else pd.DataFrame()


def efeito_amostragem(registros: pd.DataFrame, tamanhos=AMOSTRAS_COMMITS) -> pd.DataFrame:
    """Semana 3, efeito da amostragem na estrutura: refaz a rede só com os N commits mais recentes.

    Ler só o histórico recente é uma amostra enviesada (privilegia quem está ativo hoje), e esta tabela mostra
    quanto cada métrica depende desse recorte.
    """
    commits = registros.drop_duplicates("commit").sort_values("ts", ascending=False).commit.to_numpy()
    total = len(commits)
    n_arq_total = registros.arquivo.nunique()
    linhas = []
    for n in [t for t in tamanhos if t < total] + [total]:
        sub = registros[registros.commit.isin(set(commits[:n]))]
        ar = sub.groupby(["autor", "arquivo"]).size().reset_index(name="c")
        g_f = ar.groupby("arquivo").size(); g_a = ar.groupby("autor").size()
        principal = g_a.idxmax()
        dele = set(ar[ar.autor == principal].arquivo)
        orf = ((g_f == 1) & g_f.index.isin(dele)).sum()
        linhas.append({"commits": int(n), "pct_do_historico": n / total * 100, "autores": int(len(g_a)), "arquivos": int(len(g_f)),
                       "arquivos_vistos_pct": len(g_f) / n_arq_total * 100, "densidade_pct": len(ar) / (len(g_a) * len(g_f)) * 100,
                       "autores_1_arquivo_pct": (g_a == 1).mean() * 100, "arquivos_1_autor_pct": (g_f == 1).mean() * 100,
                       "autor_principal": principal, "arquivos_do_principal_pct": g_a.max() / len(g_f) * 100,
                       "orfaos_sem_principal_pct": orf / len(g_f) * 100})
    return pd.DataFrame(linhas)


# ----------------------------------------------------------------------------- análise principal
def analisar(arestas: pd.DataFrame, registros: pd.DataFrame | None = None, progress=None) -> dict:
    p = progress or (lambda f, m: None)
    autores = sorted(arestas.autor.unique())
    arquivos = sorted(arestas.arquivo.unique())
    n_a, n_f, m = len(autores), len(arquivos), len(arestas)
    if n_a < MIN_AUTORES or n_f < MIN_ARQUIVOS:
        raise ErroAnalise(f"Dados insuficientes depois da limpeza ({n_a} autores e {n_f} arquivos). "
                          f"São necessários pelo menos {MIN_AUTORES} de cada.")
    rng = np.random.default_rng(SEED)

    p(0.5, "Construindo a rede bipartida…")
    B = nx.Graph()
    B.add_nodes_from(("a:" + a, {"bipartite": 0}) for a in autores)
    B.add_nodes_from(("f:" + f, {"bipartite": 1}) for f in arquivos)
    B.add_weighted_edges_from(("a:" + r.autor, "f:" + r.arquivo, r.commits) for r in arestas.itertuples())
    A_nodes = ["a:" + a for a in autores]

    p(0.55, "Centralidades e k-core no bipartido…")
    cg = bipartite.degree_centrality(B, A_nodes)
    btw = nx.betweenness_centrality(B, k=min(FONTES_AMOSTRA, B.number_of_nodes()), seed=SEED, normalized=True)
    core = nx.core_number(B)

    ma = pd.DataFrame({
        "autor": autores,
        "arquivos_tocados": [B.degree("a:" + a) for a in autores],
        "commits": [B.degree("a:" + a, weight="weight") for a in autores],   # grau ponderado (força)
        "centralidade_grau": [cg["a:" + a] for a in autores],
        "betweenness": [btw["a:" + a] for a in autores],
        "core_number": [core["a:" + a] for a in autores]})

    deg_arquivo = pd.Series({f: B.degree("f:" + f) for f in arquivos})
    tot = arestas.groupby("arquivo").commits.sum()
    topo = arestas.loc[arestas.groupby("arquivo").commits.idxmax()].set_index("arquivo")
    mf = pd.DataFrame({
        "n_autores": deg_arquivo, "commits": tot, "autor_dominante": topo.autor,
        "fatia_dominante": topo.commits / tot,
        "core_number": pd.Series({f: core["f:" + f] for f in arquivos})}).reindex(arquivos)
    mf["critico"] = marcar_criticos(mf)
    por_autor = mf[mf.critico].autor_dominante.value_counts()
    ma["arquivos_criticos"] = ma.autor.map(por_autor).fillna(0).astype(int)

    temporal = "ultimo_ts" in arestas
    info_t = {}
    if temporal:
        ativo, ativos_arq, info_t = metricas_temporais(arestas)
        ma["ativo"] = ma.autor.map(ativo).astype(bool)
        ma["ultimo_commit"] = ma.autor.map(arestas.groupby("autor").ultimo_ts.max()).map(
            lambda t: dt.datetime.fromtimestamp(int(t), dt.timezone.utc).date().isoformat())
        mf["autores_ativos"] = ativos_arq.reindex(arquivos).fillna(0).astype(int)
        mf["dono_ativo"] = mf.autor_dominante.map(ativo).astype(bool)

    p(0.62, "Projeção autor–autor (Jaccard e esqueleto)…")
    M = matriz_incidencia(arestas, arquivos, autores)
    W = matriz_incidencia(arestas, arquivos, autores, peso="commits")
    S, info_p = projecao_autores(M, autores)
    p(0.68, "Centralidades, k-core e k-truss na projeção…")
    mp, info_m, gcc_s = metricas_projecao(S, autores)
    ma = ma.join(mp, on="autor")

    p(0.76, "Simulando a saída de autores (4 estratégias)…")
    K = max(1, int(0.20 * n_a))
    ordem_grau = np.argsort(-np.asarray(M.sum(0)).ravel(), kind="stable")
    ordem_btw = np.lexsort((-ma.arquivos_tocados.to_numpy(), -ma.betweenness_proj.to_numpy()))
    curva, n_trials, ordem_gul = curvas_remocao(M, W, {"grau": ordem_grau, "betweenness": ordem_btw}, K, rng)

    p(0.86, "Eficiência da projeção antes e depois das saídas…")
    fontes = list(rng.choice(list(gcc_s), size=min(FONTES_AMOSTRA, gcc_s.number_of_nodes()), replace=False)) \
        if gcc_s.number_of_nodes() else []
    def efic_sem(rem):
        H = S.copy(); H.remove_nodes_from(rem)
        return eficiencia(H, [f for f in fontes if f not in rem])
    efic = {"efic_base": eficiencia(S, fontes)}
    top_grau = [autores[j] for j in ordem_grau[:5]]
    top_btw = [autores[j] for j in ordem_btw[:5]]
    efic.update({"efic_sem_top1_grau": efic_sem(top_grau[:1]), "efic_sem_top5_grau": efic_sem(top_grau),
                 "efic_sem_top1_btw": efic_sem(top_btw[:1]), "efic_sem_top5_btw": efic_sem(top_btw)})

    p(0.9, "Sucessores, sensibilidade, subredes e amostragem…")
    mf_out = mf.reset_index(names="arquivo")
    suc = sucessores(arestas, mf_out, ma)
    sens = sensibilidade(mf)
    pastas = subredes_por_pasta(arestas, mf_out)
    amostra = efeito_amostragem(registros) if registros is not None else None

    comps = list(nx.connected_components(B))
    gcc_pct = max(len(c) for c in comps) / B.number_of_nodes() * 100
    H = B.copy(); H.remove_node("a:" + autores[ordem_grau[0]])
    H.remove_nodes_from([n for n in list(H) if H.degree(n) == 0])
    comps_h = list(nx.connected_components(H)) if H.number_of_nodes() else []
    gcc_sem = (max(len(c) for c in comps_h) / H.number_of_nodes() * 100) if comps_h else 0.0

    def autores_para(col, frac):
        ok = np.where(curva[col].to_numpy() >= frac * 100)[0]
        return int(ok[0]) if len(ok) else None

    principal = ma.sort_values("arquivos_tocados", ascending=False).iloc[0]
    no_esq = ma[ma.grau_proj > 0]
    el = int((mf.commits >= MIN_COMMITS_CRITICO).sum())
    resumo = {
        "n_autores": n_a, "n_arquivos": n_f, "n_arestas": m, "densidade_pct": m / (n_a * n_f) * 100,
        "componentes": len(comps), "gcc_pct": gcc_pct, "gcc_sem_principal_pct": gcc_sem,
        "k_max": int(max(core.values())),
        "pct_autores_1_arquivo": float((ma.arquivos_tocados == 1).mean() * 100),
        "pct_arquivos_1_autor": float((mf.n_autores == 1).mean() * 100),
        "autor_principal": str(principal.autor), "pct_arquivos_autor_principal": float(principal.arquivos_tocados / n_f * 100),
        "n_criticos": int(mf.critico.sum()), "n_elegiveis": el,
        "orfaos_1_autor_pct": float(curva.orfaos_direcionada_pct.iloc[1]),
        "orfaos_1_autor_aleatoria_pct": float(curva.orfaos_aleatoria_pct.iloc[1]),
        "conhecimento_1_autor_pct": float(curva.conhecimento_direcionada_pct.iloc[1]),
        "autores_para_25pct_orfaos": autores_para("orfaos_direcionada_pct", 0.25),
        "autores_para_50pct_orfaos": autores_para("orfaos_direcionada_pct", 0.50),
        "gulosa_autores_para_25pct": autores_para("orfaos_gulosa_pct", 0.25),
        "gulosa_autores_para_50pct": autores_para("orfaos_gulosa_pct", 0.50),
        "betweenness_autores_para_50pct": autores_para("orfaos_betweenness_pct", 0.50),
        "conhecimento_autores_para_50pct": autores_para("conhecimento_direcionada_pct", 0.50),
        "gulosa_primeiros": [autores[j] for j in ordem_gul[:5]],
        "tentativas_aleatorias": n_trials,
        "spearman_grau_btw_bipartido": float(ma[["arquivos_tocados", "betweenness"]].corr("spearman").iloc[0, 1]),
        "spearman_grau_btw_projecao": float(no_esq[["arquivos_tocados", "betweenness_proj"]].corr("spearman").iloc[0, 1])
        if len(no_esq) > 2 else None,
        "top10_grau_e_btw_proj": int(len(set(ma.nlargest(10, "arquivos_tocados").autor) & set(ma.nlargest(10, "betweenness_proj").autor))),
        "principal_no_nucleo": bool(ma.set_index("autor").nucleo.get(principal.autor, False)),
        "autores_nucleo": sorted(ma[ma.nucleo].autor.tolist())[:40],
        **info_p, **info_m, **efic, **info_t,
    }
    if temporal:
        resumo.update({"pct_arquivos_sem_autor_ativo": float((mf.autores_ativos == 0).mean() * 100),
                       "criticos_dono_inativo": int((mf.critico & ~mf.dono_ativo).sum()),
                       "principal_ativo": bool(ma.set_index("autor").ativo[principal.autor])})
    return {"arestas": arestas, "ma": ma, "mf": mf_out, "curva": curva, "resumo": resumo,
            "sucessores": suc, "sensibilidade": sens, "pastas": pastas, "amostragem": amostra}


# ----------------------------------------------------------------------------- persistência
ARQUIVOS = {"arestas": "arestas_autor_arquivo.csv", "ma": "metricas_autores.csv", "mf": "metricas_arquivos.csv",
            "curva": "curva_remocao.csv", "sucessores": "sucessores.csv", "sensibilidade": "sensibilidade.csv",
            "pastas": "subredes_pastas.csv", "amostragem": "efeito_amostragem.csv"}


def _json_padrao(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    raise TypeError(type(o))


def salvar(pasta: Path, res: dict, extra: dict) -> None:
    pasta.mkdir(parents=True, exist_ok=True)
    for chave, nome in ARQUIVOS.items():
        if res.get(chave) is not None:
            res[chave].to_csv(pasta / nome, index=False)
    (pasta / "resumo.json").write_text(json.dumps({**extra, **res["resumo"]}, ensure_ascii=False, indent=2, default=_json_padrao),
                                       encoding="utf-8")


def carregar(pasta: Path) -> dict:
    """Lê um projeto salvo. Projetos analisados por versões antigas não têm os CSVs novos: a chave vem como None."""
    out = {chave: (pd.read_csv(pasta / nome) if (pasta / nome).exists() else None) for chave, nome in ARQUIVOS.items()}
    out["resumo"] = json.loads((pasta / "resumo.json").read_text(encoding="utf-8"))
    return out


def listar_projetos(raiz: Path = PASTA_PROJETOS) -> list[dict]:
    out = []
    for r in sorted(raiz.glob("*/resumo.json")):
        try:
            d = json.loads(r.read_text(encoding="utf-8")); d["slug"] = r.parent.name; d["mtime"] = r.stat().st_mtime
            out.append(d)
        except Exception:
            continue
    out.sort(key=lambda d: (not d.get("padrao", False), d.get("repo", "").lower() != PROJETO_INICIAL, d.get("repo", "").lower()))
    return out


def analisar_repositorio(entrada: str, max_commits: int | None = None, raiz: Path = PASTA_PROJETOS,
                         padrao: bool = False, progress=None) -> str:
    """Pipeline completo: coleta → limpeza → análise → salva. Devolve o slug do projeto."""
    p = progress or (lambda f, m: None)
    dono, repo, url = normalizar_entrada(entrada)
    df, arquivos_head, meta = coletar_historico(url, max_commits, p)
    arestas, funil, registros = limpar(df, arquivos_head)
    res = analisar(arestas, registros, p)
    extra = {"repo": f"{dono}/{repo}", "url": f"https://github.com/{dono}/{repo}", "padrao": padrao,
             "coletado_em": dt.date.today().isoformat(), **meta, **{f"funil_{k}": v for k, v in funil.items()}}
    s = slug(dono, repo)
    salvar(raiz / s, res, extra)
    p(1.0, "Pronto.")
    return s


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repositorio", help="dono/repo ou link do GitHub")
    ap.add_argument("--max-commits", type=int, default=None, help="analisa só os N commits mais recentes")
    ap.add_argument("--padrao", action="store_true", help="marca como projeto padrão")
    a = ap.parse_args()
    try:
        s = analisar_repositorio(a.repositorio, a.max_commits, padrao=a.padrao,
                                 progress=lambda f, m: print(f"[{f:4.0%}] {m}", flush=True))
        print("salvo em", PASTA_PROJETOS / s)
    except ErroAnalise as e:
        sys.exit(f"Erro: {e}")
