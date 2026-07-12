# Monitor de Leilões — Sorocaba/SP

Script em Python que varre portais de leilão de imóveis, **descarta anúncios
com praça já encerrada** (o problema real: os sites mantêm lotes vencidos no
ar) e ranqueia o que sobrou por distância do trabalho.

## O que ele faz

1. Coleta lotes em vários portais via *adaptadores* plugáveis:
   - **Caixa** — lê o CSV oficial e gratuito de imóveis de SP (funcional).
   - **Destak** — descobre lotes de Sorocaba pelo `sitemap.xml` e faz o parse
     de specs, preço, geolocalização e link do edital.
   - **Spy** e **Mega** — esqueleto pronto, marcado com `#TODO` para plugar o
     endpoint/seletor real de cada site.
2. Aplica os filtros do perfil (casa inteira, 2 quartos, 2 vagas, teto de
   preço, sem cota-parte/direitos).
3. **Filtro de data** (o mais importante): quando a página não traz a data de
   fechamento, baixa o **PDF do edital** e usa a maior data de praça
   encontrada. Lotes com a última praça no passado são descartados.
4. Calcula a distância (haversine) até o local de trabalho e ranqueia por
   proximidade e, em seguida, por menor preço.

## Instalação

```bash
pip install -r requirements.txt
```

## Uso

```bash
python monitor_leiloes_sorocaba.py            # imprime a tabela e salva o CSV
python monitor_leiloes_sorocaba.py --csv out.csv
```

## Configuração

Todos os parâmetros de busca ficam no dicionário `CONFIG`, no topo do script
(coordenadas do trabalho, raio máximo, teto de preço, mínimo de quartos/vagas,
etc.).

## Agendamento (1x ao dia)

- **cron (Linux):**
  ```
  0 8 * * * cd /caminho && /usr/bin/python3 monitor_leiloes_sorocaba.py
  ```
- **GitHub Actions:** `schedule: - cron: "0 11 * * *"` (11h UTC = 8h BRT).

Para receber alertas, plugue e-mail (`smtplib`) ou Telegram no final do
pipeline — há um exemplo comentado no rodapé do script.

## Observação técnica

Cada portal muda de layout e tem proteção anti-bot. O núcleo (filtro por data
futura, leitura de edital em PDF, haversine e ranking) já está pronto; os
adaptadores marcados com `#TODO` precisam do endpoint/seletor real do site
antes de serem habilitados. A sugestão é começar com um portal (Caixa ou
Destak) e ir somando.
