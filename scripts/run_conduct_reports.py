import asyncio, sys, os, time, json, traceback
sys.path.insert(0, '/Users/sanek/project_with_OpenClaw/bank_audit_platform/src')
from dotenv import load_dotenv
load_dotenv('/Users/sanek/project_with_OpenClaw/bank_audit_platform/.env')
import logging
logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
# приглушим шумные логгеры
for noisy in ['httpx','openai','urllib3','ddgs','primp']:
    logging.getLogger(noisy).setLevel(logging.ERROR)

from bank_audit.research.conduct_research import run_conduct_research, CONDUCT_PRODUCTS, _make_client
from bank_audit.web.pdf_export import export_report_to_pdf

OUT='/Users/sanek/project_with_OpenClaw/bank_audit_platform/workspace/conduct_reports'
os.makedirs(OUT, exist_ok=True)

# Порядок как в письме
ORDER = ['credit_card','consumer_loan','mortgage','auto_loan','edu_loan',
         'broker_account','iis','securities','salary','debit_card','deposit',
         'subscription','transfers','insurance','pension','premium']

async def main():
    client = _make_client()
    summary = []
    t_all = time.time()
    for i, pk in enumerate(ORDER, 1):
        label = CONDUCT_PRODUCTS[pk]['label']
        t0 = time.time()
        # RESUME: пропускаем уже готовые (есть .pdf + .json) — экономия API при перезапуске
        if os.path.exists(f'{OUT}/{pk}.pdf') and os.path.exists(f'{OUT}/{pk}.json'):
            print(f"\n[{i}/16] {label} ({pk}) — уже готов, пропуск", flush=True)
            try:
                r = json.load(open(f'{OUT}/{pk}.json'))
                summary.append({'pk':pk,'label':label,'cases':len(r.get('cases',[])),
                                'complaints':len(r.get('complaints',[])),
                                'sources':len(r.get('sources',[])),'pdf_kb':0,'sec':0,'skipped':True})
            except Exception:
                summary.append({'pk':pk,'label':label,'skipped':True})
            continue
        print(f"\n{'='*70}\n[{i}/16] {label} ({pk})\n{'='*70}", flush=True)
        try:
            res = await run_conduct_research(pk, client=client)
            md, srcs = res['report_md'], res['sources']
            nc, nco = len(res['cases']), len(res['complaints'])
            open(f'{OUT}/{pk}.md','w').write(md)
            json.dump(res, open(f'{OUT}/{pk}.json','w'), ensure_ascii=False, indent=2)
            pdf = await asyncio.get_event_loop().run_in_executor(None,
                lambda: export_report_to_pdf(
                    question=f"Риск поведения по рекламе: {label}",
                    report_md=md, sources=srcs, meta={"audit_id": f"conduct_{pk}"},
                    verification={"checked": True,
                        "note": "Кейсы подтверждены дословными цитатами из источников"},
                    charts=[]))
            open(f'{OUT}/{pk}.pdf','wb').write(pdf)
            dt = time.time()-t0
            summary.append({'pk':pk,'label':label,'cases':nc,'complaints':nco,
                            'sources':len(srcs),'pdf_kb':len(pdf)//1024,'sec':round(dt)})
            print(f"  ✓ {nc} кейсов, {nco} жалоб, {len(srcs)} ист. → {pk}.pdf ({len(pdf)//1024}KB) [{dt:.0f}s]", flush=True)
        except Exception as e:
            summary.append({'pk':pk,'label':label,'error':str(e)[:200]})
            print(f"  ✗ FAILED: {e}", flush=True)
            traceback.print_exc()
    # Итоговая сводка
    print(f"\n\n{'#'*70}\nИТОГО за {(time.time()-t_all)/60:.1f} мин\n{'#'*70}", flush=True)
    json.dump(summary, open(f'{OUT}/_summary.json','w'), ensure_ascii=False, indent=2)
    tot_c = sum(s.get('cases',0) for s in summary)
    tot_co = sum(s.get('complaints',0) for s in summary)
    for s in summary:
        if 'error' in s:
            print(f"  ✗ {s['label']:42} ERROR {s['error'][:60]}")
        else:
            print(f"  ✓ {s['label']:42} {s['cases']:2} кейсов · {s['complaints']:2} жалоб · {s['pdf_kb']:4}KB")
    print(f"\nВСЕГО: {tot_c} кейсов, {tot_co} жалоб, {sum(1 for s in summary if 'error' not in s)}/16 PDF")
    print(f"Папка: {OUT}")

asyncio.run(main())
