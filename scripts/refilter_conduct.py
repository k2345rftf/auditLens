"""API-free перефильтрация + перерисовка conduct-отчётов из сохранённых .json.
Применяет усиленный product-фильтр (concept-groups) к уже извлечённым кейсам/
жалобам, отсевает чужие продукты, перенумеровывает источники, пересобирает MD+PDF.
LLM не вызывается — только Chromium для PDF."""
import sys, os, json
sys.path.insert(0, '/Users/sanek/project_with_OpenClaw/bank_audit_platform/src')
from dotenv import load_dotenv; load_dotenv('/Users/sanek/project_with_OpenClaw/bank_audit_platform/.env')
from bank_audit.research.conduct_research import (
    ConductCase, ConductComplaint, ConductSource, classify_case_product,
    _prune_and_renumber, render_conduct_report)
from bank_audit.web.pdf_export import export_report_to_pdf

OUT='/Users/sanek/project_with_OpenClaw/bank_audit_platform/workspace/conduct_reports'
ORDER=['credit_card','consumer_loan','mortgage','auto_loan','edu_loan','broker_account',
       'iis','securities','salary','debit_card','deposit','subscription','transfers',
       'insurance','pension','premium']

def _cf(d, cls):
    return cls(**{k:v for k,v in d.items() if k in cls.__dataclass_fields__})

print(f'{"категория":22} {"было":>10} {"стало":>10}  отсеяно')
for pk in ORDER:
    p=f'{OUT}/{pk}.json'
    if not os.path.exists(p): print(f'{pk:22}  нет json'); continue
    res=json.load(open(p)); label=res.get('product_label',pk)
    cases=[_cf(c,ConductCase) for c in res.get('cases',[])]
    comps=[_cf(c,ConductComplaint) for c in res.get('complaints',[])]
    srcs=[ConductSource(n=s.get('n',0),url=s.get('url',''),title=s.get('title',''),
          domain=s.get('domain',''),text='',trust=s.get('trust_score',0.5),
          snippet=(s.get('excerpts') or [''])[0]) for s in res.get('sources',[])]
    nc0,nco0=len(cases),len(comps)
    # фильтр кейсов
    cases=[c for c in cases if classify_case_product(
            f"{c.violation} {c.verbatim_quote} {c.ad_channel}", pk)=='own']
    # фильтр жалоб
    comps=[c for c in comps if classify_case_product(
            f"{c.summary} {c.ad_issue} {c.verbatim_quote}", pk)!='foreign']
    # отсев источников до цитируемых + перенумерация
    cases,comps,kept=_prune_and_renumber(cases,comps,srcs)
    md=render_conduct_report(label,cases,comps,kept)
    open(f'{OUT}/{pk}.md','w').write(md)
    # обновим json
    res['cases']=[c.to_dict() for c in cases]; res['complaints']=[c.to_dict() for c in comps]
    res['sources']=[{'n':s.n,'url':s.url,'title':s.title,'domain':s.domain,
                     'trust_score':s.trust,'source_kind':'regulator' if s.trust>=0.9 else ('news_legal' if s.trust>=0.75 else 'aggregator'),
                     'excerpts':[s.snippet] if s.snippet else []} for s in kept]
    json.dump(res,open(p,'w'),ensure_ascii=False,indent=2)
    pdf_srcs=res['sources']
    pdf=export_report_to_pdf(question=f"Риск поведения по рекламе: {label}",
        report_md=md, sources=pdf_srcs, meta={"audit_id":f"conduct_{pk}"},
        verification={"checked":True,"note":"Кейсы подтверждены дословными цитатами; отсев чужих продуктов применён"},
        charts=[])
    open(f'{OUT}/{pk}.pdf','wb').write(pdf)
    dropped=(nc0-len(cases))+(nco0-len(comps))
    print(f'{label[:22]:22} {f"{nc0}к+{nco0}ж":>10} {f"{len(cases)}к+{len(comps)}ж":>10}  {"-"+str(dropped) if dropped else "—"}')
print('\nПерефильтрация завершена (без API).')
