"""
Satisfação dos formandos @ CESAE Digital - Análise de sentimento (v4)

Um único modelo (nlptown/bert-base-multilingual-uncased-sentiment) e um único
ficheiro de saída para o Power BI.

Fluxo (cada passo é uma função):
  1. carregar_e_anonimizar  lê o CSV, apaga colunas pessoais e valida as colunas
  2. desdobrar              uma linha por (questionário x dimensão), com a nota
  3. classificar            classifica só os textos que ainda não estão na cache
  4. juntar                 atribui o sentimento a cada linha
  5. exportar               grava powerbi_sentimento_v4.csv
  6. perguntas 5, 6 e 7     resumos impressos na consola (opcional: --validar)

Uso
---
  python analise_sentimento_v4.py                      # corre tudo
  python analise_sentimento_v4.py --validar            # + exatidão vs referência
  python analise_sentimento_v4.py --dados novos.csv --saida saida_v4/2026-11
  python analise_sentimento_v4.py --semear feedbacks_classificados_v2.csv

A cache (cache_nlptown.csv) guarda cada texto já classificado; com dados novos só
os textos novos passam pelo modelo (precisa de: pip install transformers torch).

Medidas DAX no Power BI (tabela 'Sentimento'):
  Respondentes   = CALCULATE(DISTINCTCOUNT(Sentimento[IDQuestionario]), Sentimento[Respondeu]=1)
  Adesao %       = DIVIDE(CALCULATE(DISTINCTCOUNT(Sentimento[IDQuestionario]), Sentimento[Tem_Comentario]=1), [Respondentes])
  % Negativos    = DIVIDE(CALCULATE(COUNTROWS(Sentimento), Sentimento[Sentimento]="Negativo"), CALCULATE(COUNTROWS(Sentimento), Sentimento[Tem_Comentario]=1))
  Nota por sent. = AVERAGE(Sentimento[Satisfacao])      -- com 'Sentimento' no eixo

AVISO: se Origem_Dados = SINTETICO, os resultados descrevem o gerador dos dados
e não devem ser apresentados como factos sobre formandos reais.
"""
import argparse
import os
import re
import sys

import numpy as np
import pandas as pd

# ------------------------------------------------------------------------------
# CONFIGURAÇÃO (o que muda quando entrarem dados reais)
# ------------------------------------------------------------------------------
MODELO_HF = 'nlptown/bert-base-multilingual-uncased-sentiment'
MAPA_ESTRELAS = {1: 'Negativo', 2: 'Negativo', 3: 'Neutro', 4: 'Positivo', 5: 'Positivo'}
ORDEM = ['Positivo', 'Neutro', 'Negativo']
CODIGO = {'Negativo': -1, 'Neutro': 0, 'Positivo': 1}

# dimensão -> (coluna do comentário, coluna da nota)
DIMENSOES = {
    'Formador': ('Comentario_Formador', 'Satisfacao_Formador'),
    'Modulo': ('Comentario_Modulo', 'Satisfacao_Modulo'),
    'Logistica': ('Comentario_Logistica', 'Satisfacao_Logistica'),
}
COLUNAS_OBRIGATORIAS = ['IDQuestionario', 'Respondeu_Questionario'] + [
    c for par in DIMENSOES.values() for c in par]

# Colunas pessoais: apagadas logo à leitura (o nome da coluna contém um destes termos)
TERMOS_PESSOAIS = ['nome', 'email', 'e-mail', 'nif', 'telefone', 'telemovel',
                   'telemóvel', 'contacto', 'morada', 'nascimento', 'iban']
# Só estas colunas (se existirem) seguem para o ficheiro do Power BI
COLUNAS_SAIDA = ['IDQuestionario', 'IDInscrito', 'IDAluno', 'IDAccao', 'IDModulo',
                 'IDFormador', 'IDCurso', 'Curso', 'Turma', 'Modulo', 'Formador',
                 'Polo_CESAE', 'Modalidade', 'Data_Submissao', 'Origem_Dados']

FICHEIRO_SAIDA = 'powerbi_sentimento_v4.csv'
FICHEIRO_CACHE = 'cache_nlptown.csv'


# ------------------------------------------------------------------------------
# 1. LEITURA, ANONIMIZAÇÃO E VALIDAÇÃO
# ------------------------------------------------------------------------------
def carregar_e_anonimizar(caminho):
  if not os.path.exists(caminho):
    sys.exit(f"Ficheiro '{caminho}' não encontrado.")
  df = pd.read_csv(caminho, encoding='utf-8-sig')
  em_falta = [c for c in COLUNAS_OBRIGATORIAS if c not in df.columns]
  if em_falta:
    sys.exit(f'Colunas obrigatórias em falta: {em_falta}')
  pessoais = [c for c in df.columns
              if any(t in c.lower() for t in TERMOS_PESSOAIS)
              and c not in COLUNAS_OBRIGATORIAS]
  df = df.drop(columns=pessoais)
  if pessoais:
    print(f'Anonimização: {len(pessoais)} colunas pessoais descartadas: {pessoais}')
  if df['IDQuestionario'].duplicated().any():
    sys.exit('IDQuestionario repetido: cada linha deve ser um questionário.')
  for c in ['Respondeu_Questionario'] + [v[1] for v in DIMENSOES.values()]:
    df[c] = pd.to_numeric(df[c], errors='coerce')
  return df


def tem_texto(serie):
  return serie.notna() & (serie.astype(str).str.strip() != '')


# ------------------------------------------------------------------------------
# 2. TABELA LONGA: uma linha por questionário x dimensão
# ------------------------------------------------------------------------------
def desdobrar(df):
  ids = [c for c in COLUNAS_SAIDA if c in df.columns] + ['Respondeu_Questionario']
  partes = []
  for dim, (col_c, col_s) in DIMENSOES.items():
    p = df[ids].copy()
    p['Dimensao'] = dim
    p['Satisfacao'] = df[col_s]
    p['Comentario'] = df[col_c].where(tem_texto(df[col_c])).astype('string').str.strip()
    partes.append(p)
  longo = pd.concat(partes, ignore_index=True)
  longo['Respondeu'] = (longo.pop('Respondeu_Questionario') == 1).astype(int)
  longo['Tem_Comentario'] = longo['Comentario'].notna().astype(int)
  return longo


# ------------------------------------------------------------------------------
# 3. CLASSIFICAÇÃO COM CACHE
# ------------------------------------------------------------------------------
def ler_cache(caminho):
  if os.path.exists(caminho):
    return pd.read_csv(caminho, encoding='utf-8-sig')
  return pd.DataFrame(columns=['Comentario', 'Estrelas', 'Sentimento'])


def semear_cache(origem, destino):
  """Aproveita classificações anteriores (colunas Comentarios + Sentimento_BERT_Score)."""
  ant = pd.read_csv(origem, encoding='utf-8-sig')
  ant = ant.drop_duplicates('Comentarios')
  estrelas = ant['Sentimento_BERT_Score'].str.extract(r'(\d)')[0].astype(int)
  novo = pd.DataFrame({'Comentario': ant['Comentarios'].str.strip(), 'Estrelas': estrelas})
  novo['Sentimento'] = novo['Estrelas'].map(MAPA_ESTRELAS)
  cache = pd.concat([ler_cache(destino), novo]).drop_duplicates('Comentario')
  cache.to_csv(destino, index=False, encoding='utf-8-sig')
  print(f'Cache semeada: {len(cache)} textos em {destino}')


def _correr_modelo(textos, lote=16):
  try:
    from transformers import pipeline
  except ImportError:
    sys.exit(f'{len(textos)} textos novos precisam do modelo: pip install transformers torch')
  print(f'A carregar {MODELO_HF} (a 1.ª vez faz download)...')
  clf = pipeline('sentiment-analysis', model=MODELO_HF, truncation=True, max_length=128)
  estrelas = []
  for i in range(0, len(textos), 512):
    saida = clf(textos[i:i + 512], batch_size=lote)
    estrelas += [int(r['label'][0]) for r in saida]
    print(f'  {min(i + 512, len(textos))}/{len(textos)} textos')
  return estrelas


def classificar(longo, caminho_cache=FICHEIRO_CACHE):
  """Devolve a cache atualizada; só os textos novos passam pelo modelo."""
  cache = ler_cache(caminho_cache)
  unicos = longo['Comentario'].dropna().unique().tolist()
  novos = [t for t in unicos if t not in set(cache['Comentario'])]
  print(f'{len(unicos)} textos únicos | {len(unicos) - len(novos)} já na cache | {len(novos)} novos')
  if novos:
    est = _correr_modelo(novos)
    add = pd.DataFrame({'Comentario': novos, 'Estrelas': est})
    add['Sentimento'] = add['Estrelas'].map(MAPA_ESTRELAS)
    cache = pd.concat([cache, add], ignore_index=True)
    cache.to_csv(caminho_cache, index=False, encoding='utf-8-sig')
  return cache


# ------------------------------------------------------------------------------
# 4. JUNTAR E EXPORTAR
# ------------------------------------------------------------------------------
def juntar(longo, cache):
  t = longo.merge(cache[['Comentario', 'Estrelas', 'Sentimento']], on='Comentario', how='left')
  t.loc[(t['Respondeu'] == 1) & (t['Tem_Comentario'] == 0), 'Sentimento'] = 'Sem comentário'
  t.loc[t['Respondeu'] == 0, 'Sentimento'] = 'Não respondeu'
  t['Sentimento_Valor'] = t['Sentimento'].map(CODIGO)
  assert len(t) == len(longo), 'a junção duplicou linhas'
  assert t.loc[t['Tem_Comentario'] == 1, 'Sentimento'].isin(ORDEM).all(), 'comentário sem sentimento'
  return t


def exportar(t, pasta):
  os.makedirs(pasta, exist_ok=True)
  out = t.copy()
  for c in ['Satisfacao', 'Estrelas', 'Sentimento_Valor']:
    out[c] = out[c].astype('Int64')       # inteiros: evita problemas de separador decimal no Power BI
  ordem = [c for c in COLUNAS_SAIDA if c in out.columns] + [
      'Dimensao', 'Respondeu', 'Satisfacao', 'Tem_Comentario', 'Comentario',
      'Estrelas', 'Sentimento', 'Sentimento_Valor']
  caminho = os.path.join(pasta, FICHEIRO_SAIDA)
  out[ordem].to_csv(caminho, index=False, encoding='utf-8-sig')
  print(f'Ficheiro para o Power BI: {caminho} ({len(out)} linhas, {len(ordem)} colunas)')
  return caminho


# ------------------------------------------------------------------------------
# 5. PERGUNTAS 5, 6 E 7
# ------------------------------------------------------------------------------
def pergunta5(t):
  q = t.drop_duplicates('IDQuestionario')
  n_total, n_resp = len(q), int(q['Respondeu'].sum())
  qualquer = t[t['Tem_Comentario'] == 1]['IDQuestionario'].nunique()
  linhas = [{'Indicador': 'Taxa de resposta ao questionário', 'n': n_resp,
             '%': round(n_resp / n_total * 100, 1), 'Base': n_total},
            {'Indicador': 'Pelo menos 1 comentário', 'n': qualquer,
             '%': round(qualquer / n_resp * 100, 1), 'Base': n_resp}]
  for dim in DIMENSOES:
    n = int(t[(t['Dimensao'] == dim)]['Tem_Comentario'].sum())
    linhas.append({'Indicador': f'Comentário em {dim}', 'n': n,
                   '%': round(n / n_resp * 100, 1), 'Base': n_resp})
  return pd.DataFrame(linhas)


def so_comentarios(t):
  return t[t['Tem_Comentario'] == 1]


def pergunta6(t):
  c = so_comentarios(t)
  tab = (c.groupby('Dimensao')['Sentimento'].value_counts(normalize=True).mul(100)
         .unstack().reindex(columns=ORDEM).fillna(0).round(1))
  tab['n'] = c.groupby('Dimensao').size()
  return tab


def pergunta7(t):
  c = so_comentarios(t)
  linhas = []
  for dim, g in c.groupby('Dimensao'):
    rho = g['Sentimento_Valor'].astype(float).corr(g['Satisfacao'].astype(float), method='spearman')
    for s in ORDEM:
      x = g.loc[g['Sentimento'] == s, 'Satisfacao']
      linhas.append({'Dimensao': dim, 'Sentimento': s, 'n': len(x),
                     'Media': round(x.mean(), 2), 'Mediana': x.median(),
                     'Spearman': round(rho, 3)})
  return pd.DataFrame(linhas)


# ------------------------------------------------------------------------------
# 6. VALIDAÇÃO (opcional)
# ------------------------------------------------------------------------------
def _nomes_a_mascarar(df):
  nomes = set()
  for col in ['Modulo', 'Curso', 'Area_Especializacao', 'Area_Formacao']:
    if col in df.columns:
      nomes |= set(df[col].dropna().astype(str))
  return sorted([n for n in nomes if len(n) >= 3], key=len, reverse=True)


def referencia(t, df, equipa='amostra_validacao_final.csv', provisoria='referencia_provisoria.csv'):
  """(nome, DataFrame[Comentario, Ref]) por comentário escrito, ou (None, None)."""
  c = so_comentarios(t)[['Dimensao', 'Comentario']]
  if os.path.exists(equipa):
    a = pd.read_csv(equipa)[['Comentario', 'Ref']]
    return 'rótulos da equipa', c.merge(a, on='Comentario', how='left')[['Comentario', 'Ref']]
  if os.path.exists(provisoria):
    prov = pd.read_csv(provisoria)
    pad = re.compile('|'.join(re.escape(n) for n in _nomes_a_mascarar(df)))
    c = c.assign(modelo_frase=c['Comentario'].map(lambda x: pad.sub('{X}', x)))
    c = c.merge(prov[['Dimensao', 'modelo_frase', 'Rotulo']], on=['Dimensao', 'modelo_frase'], how='left')
    return 'referência provisória (88 modelos de frase)', c.rename(columns={'Rotulo': 'Ref'})[['Comentario', 'Ref']]
  return None, None


def validar(t, ref):
  from sklearn.metrics import cohen_kappa_score, f1_score
  m = ref.merge(so_comentarios(t)[['Comentario', 'Sentimento']].drop_duplicates('Comentario'),
                on='Comentario', how='left').dropna(subset=['Ref'])
  r, p = m['Ref'], m['Sentimento']
  met = {'n': len(m), 'Exatidao_%': round((r == p).mean() * 100, 1),
         'F1_macro': round(f1_score(r, p, labels=ORDEM, average='macro'), 3),
         'Recall_Negativo_%': round((p[r == 'Negativo'] == 'Negativo').mean() * 100, 1),
         'Kappa': round(cohen_kappa_score(r, p), 3)}
  conf = pd.crosstab(r, p, rownames=['Ref.'], colnames=['Previsto']).reindex(
      index=ORDEM, columns=ORDEM, fill_value=0)
  assert conf.values.sum() == len(m)
  return met, conf


# ------------------------------------------------------------------------------
# EXECUÇÃO
# ------------------------------------------------------------------------------
def main():
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument('--dados', default='dados_projeto.csv')
  ap.add_argument('--saida', default='saida_v4')
  ap.add_argument('--cache', default=FICHEIRO_CACHE)
  ap.add_argument('--semear', metavar='CSV', help='classificações anteriores a aproveitar')
  ap.add_argument('--validar', action='store_true')
  args = ap.parse_args()

  if args.semear:
    semear_cache(args.semear, args.cache)

  df = carregar_e_anonimizar(args.dados)
  print(f'=== {len(df)} questionários | {int((df["Respondeu_Questionario"] == 1).sum())} respondentes ===')
  if 'Origem_Dados' in df.columns and df['Origem_Dados'].astype(str).str.upper().eq('SINTETICO').any():
    print('AVISO: dados sintéticos - não apresentar como resultados reais.')

  longo = desdobrar(df)
  cache = classificar(longo, args.cache)
  t = juntar(longo, cache)
  exportar(t, args.saida)

  print('\n--- PERGUNTA 5: adesão (base: respondentes) ---')
  print(pergunta5(t).to_string(index=False))
  print('\n--- PERGUNTA 6: sentimento por dimensão (% dos comentários) ---')
  print(pergunta6(t).to_string())
  print('\n--- PERGUNTA 7: nota por sentimento ---')
  print(pergunta7(t).to_string(index=False))

  if args.validar:
    nome, ref = referencia(t, df)
    if ref is None:
      print('\nSem referência (referencia_provisoria.csv ou amostra_validacao_final.csv).')
    else:
      met, conf = validar(t, ref)
      print(f'\n--- VALIDAÇÃO contra {nome} ---')
      print(met)
      print(conf.to_string())
  print('\nConcluído.')


if __name__ == '__main__':
  main()
