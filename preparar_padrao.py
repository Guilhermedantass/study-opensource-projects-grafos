"""Gera os projetos que já vêm analisados no app. Uso: python preparar_padrao.py"""
import time
from analise import analisar_repositorio, ErroAnalise

PADRAO = [("pallets/flask", None), ("pallets/click", None), ("psf/requests", None),
          ("fastapi/fastapi", None), ("django/django", 15000), ("git/git", None)]

for repo, limite in PADRAO:
    t = time.time()
    try:
        s = analisar_repositorio(repo, limite, padrao=True, progress=lambda f, m: print(f"  [{f:4.0%}] {m}", flush=True))
        print(f"OK  {repo} ({time.time()-t:.0f}s)", flush=True)
    except ErroAnalise as e:
        print(f"ERRO {repo}: {e}", flush=True)
    except Exception as e:
        print(f"FALHA {repo}: {type(e).__name__}: {e}", flush=True)
