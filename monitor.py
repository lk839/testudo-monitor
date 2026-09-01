#!/usr/bin/env python3
import itertools, json, os, random, re, sys, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup

TERM=os.getenv("TESTUDO_TERM","202608")
NTFY_TOPIC=os.getenv("NTFY_TOPIC","").strip()
CONFIG_PATH=Path("config.json")
STATE_PATH=Path("state.json")
META_PATH=Path("meta.json")
RULES_PATH=Path("schedule_rules.json")
REQUEST_TIMEOUT=20
SESSION=requests.Session()
SESSION.headers.update({"User-Agent":"Mozilla/5.0 AppleWebKit/537.36 Chrome/150 Safari/537.36"})

def load_json(p,d):
    try:return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:return d
def save_json(p,o): p.write_text(json.dumps(o,indent=2,sort_keys=True)+"\n",encoding="utf-8")
def course_url(c):
    dept=re.match(r"^[A-Z]+",c).group(0)
    return f"https://app.testudo.umd.edu/soc/{TERM}/{dept}/{c}"
def get_page(url):
    r=SESSION.get(url,params={"_fresh":str(int(time.time()))},
        headers={"Cache-Control":"no-cache, no-store, max-age=0","Pragma":"no-cache"},
        timeout=REQUEST_TIMEOUT)
    if r.status_code in (403,429): raise RuntimeError(f"ACCESS_CONTROL_{r.status_code}")
    if 500<=r.status_code<=599: raise RuntimeError(f"SERVER_{r.status_code}")
    r.raise_for_status(); return r.text

def parse_clock(s):
    m=re.fullmatch(r"\s*(\d{1,2}):(\d{2})(am|pm)\s*",s,re.I)
    if not m:return None
    h,mi=int(m.group(1)),int(m.group(2)); ap=m.group(3).lower()
    if h==12:h=0
    if ap=="pm":h+=12
    return h*60+mi
def hm_to_min(s):
    h,m=map(int,s.split(":")); return h*60+m

DAYMAP={"M":["M"],"Tu":["Tu"],"W":["W"],"Th":["Th"],"F":["F"],
        "MW":["M","W"],"MF":["M","F"],"WF":["W","F"],"MWF":["M","W","F"],
        "TuTh":["Tu","Th"],"MTh":["M","Th"],"MTu":["M","Tu"],
        "WTh":["W","Th"],"ThF":["Th","F"]}
def expand_days(tok):
    if tok in DAYMAP:return DAYMAP[tok]
    out=[]; i=0
    while i<len(tok):
        if tok.startswith("Tu",i):out.append("Tu");i+=2
        elif tok.startswith("Th",i):out.append("Th");i+=2
        elif tok[i] in "MWF":out.append(tok[i]);i+=1
        else:i+=1
    return out

def scope_course_text(text,course):
    pat=re.compile(rf"(?<![A-Za-z0-9]){re.escape(course)}(?![A-Za-z0-9]).{{0,250}}?Syllabus Repository",re.I|re.S)
    sm=pat.search(text)
    if not sm:return text
    start=sm.start(); end=len(text)
    nxt=re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,5}\d{3,4}[A-Z]?)(?![A-Za-z0-9]).{0,250}?Syllabus Repository",re.I|re.S)
    nm=nxt.search(text,sm.end())
    if nm:end=nm.start()
    return text[start:end]

def parse_page(html,course):
    soup=BeautifulSoup(html,"html.parser")
    text=soup.get_text(" ",strip=True)
    m=re.search(r"Open Seats as of\s+(\d{1,2}/\d{1,2}/\d{4}\s+at\s+\d{1,2}:\d{2}\s+[AP]M)",text,re.I)
    stamp=" ".join(m.group(1).split()) if m else None
    scoped=scope_course_text(text,course)
    sr=re.compile(
      r"(?<![A-Za-z0-9])([A-Za-z0-9]{4})(?![A-Za-z0-9])\s*(?:\*+\s*)?"
      r"([A-Za-zÀ-ÖØ-öø-ÿ.'’\-]+(?:\s+[A-Za-zÀ-ÖØ-öø-ÿ.'’\-]+){0,7})"
      r"\s+Seats\s*\(\s*Total:\s*\d+\s*,\s*Open:\s*(\d+)",re.I|re.S)
    starts=list(sr.finditer(scoped)); out={}
    tr=re.compile(r"\b(MWF|TuTh|MW|MF|WF|MTh|MTu|WTh|ThF|M|Tu|W|Th|F)\s+"
                  r"(\d{1,2}:\d{2}(?:am|pm))\s*-\s*(\d{1,2}:\d{2}(?:am|pm))",re.I)
    for i,sm in enumerate(starts):
        sec=sm.group(1).upper(); instr=" ".join(sm.group(2).split()); op=int(sm.group(3))
        end=starts[i+1].start() if i+1<len(starts) else len(scoped)
        meetings=[]
        for tm in tr.finditer(scoped[sm.end():end]):
            st,en=parse_clock(tm.group(2)),parse_clock(tm.group(3))
            if st is not None and en is not None:
                meetings.append({"days":expand_days(tm.group(1)),"start":st,"end":en,
                    "label":f"{tm.group(1)} {tm.group(2)}-{tm.group(3)}"})
        out.setdefault(sec,{"open":op,"instructor":instr,"meetings":meetings})
    return stamp,out

def overlaps(a,b):
    return bool(set(a["days"])&set(b["days"])) and a["start"]<b["end"] and b["start"]<a["end"]
def group_for(c,r):return r.get("course_groups",{}).get(c,c)
def priority_for(c,r):return r.get("priority",{}).get(group_for(c,r),0)
def credits_for(c,r):return int(r.get("course_credits",{}).get(c,0))

def normalized_current_info(course,rules):
    mts=[]
    for m in rules.get("current_meetings",{}).get(course,[]):
        mts.append({"days":m["days"],"start":hm_to_min(m["start"]),"end":hm_to_min(m["end"]),
                    "label":f"{''.join(m['days'])} {m['start']}-{m['end']}"})
    return {"open":1,"instructor":rules.get("fsoc_current_instructor","") if course=="COMM107" else
            (rules.get("preferred_instructors",{}).get("CHEM231","") if course=="CHEM231" else "current"),
            "meetings":mts}

def effective_meetings(course,info,rules):
    mts=list(info.get("meetings",[]))
    if course=="CHEM231" and rules.get("stocker_flexible_discussion",False):
        pref=rules.get("preferred_instructors",{}).get("CHEM231","").lower()
        if pref and pref in info.get("instructor","").lower():
            # Stocker's lecture is the recurring multi-day meeting. Single-day discussion
            # remains visible in the alert but is not a hard scheduling constraint.
            multi=[m for m in mts if len(m.get("days",[]))>1]
            if multi:return multi
    return mts

def schedule_meetings(schedule,rules):
    out=[]
    for course,sec,info in schedule:
        for m in effective_meetings(course,info,rules):
            out.append((course,m))
    return out

def valid_schedule(schedule,rules):
    if sum(credits_for(c,rules) for c,_,_ in schedule)!=int(rules.get("target_credits",16)):
        return False
    courses={c for c,_,_ in schedule}
    if ("CHEM231" in courses) != ("CHEM232" in courses):return False
    for fixed in rules.get("fixed_courses",[]):
        if fixed not in courses:return False
    earliest=hm_to_min(rules.get("earliest_start","09:30"))
    latest=hm_to_min(rules.get("latest_end","15:50"))
    mts=schedule_meetings(schedule,rules)
    for _,m in mts:
        if m["start"]<earliest or m["end"]>latest:return False
    for i,(ca,a) in enumerate(mts):
        for cb,b in mts[i+1:]:
            if ca!=cb and overlaps(a,b):return False
    return True

def schedule_score(schedule,rules):
    cfg=rules.get("schedule_score",{})
    score=sum(priority_for(c,rules)*float(cfg.get("priority_weight",10)) for c,_,_ in schedule)
    byday={d:[] for d in ["M","Tu","W","Th","F"]}
    for c,m in schedule_meetings(schedule,rules):
        for d in m["days"]:byday.setdefault(d,[]).append((m["start"],m["end"]))
    span=gap=early=0
    for d,arr in byday.items():
        if not arr:continue
        arr=sorted(arr); span+=max(x[1] for x in arr)-min(x[0] for x in arr)
        early+=max(0,12*60-min(x[0] for x in arr))
        for a,b in zip(arr,arr[1:]):gap+=max(0,b[0]-a[1])
    score-=span*float(cfg.get("campus_span_weight",0.03))
    score-=gap*float(cfg.get("gap_weight",0.02))
    score-=early*float(cfg.get("early_start_weight",0.015))
    return score

def current_schedule(rules):
    out=[]
    for c,s in rules.get("current_sections",{}).items():
        out.append((c,s,normalized_current_info(c,rules)))
    return out

def fsoc_allowed(info,rules):
    name=info.get("instructor","").strip().lower()
    approved=[x.lower() for x in rules.get("fsoc_approved_easier_instructors",[])]
    return any(a in name or name in a for a in approved if a and name)

def choices_for_course(course,catalog,rules):
    cursec=rules.get("current_sections",{}).get(course)
    out=[]
    if cursec:out.append((course,cursec,normalized_current_info(course,rules)))
    for sec,info in catalog.get(course,{}).items():
        if info.get("open",0)>0 and sec!=cursec:
            out.append((course,sec,info))
    return out

def best_schedule(catalog,rules):
    base=current_schedule(rules)
    base_score=schedule_score(base,rules)
    candidates=[]

    # 1) Section-only improvements while retaining the same six courses.
    ch231=choices_for_course("CHEM231",catalog,rules)
    ch232=choices_for_course("CHEM232",catalog,rules)
    b207=choices_for_course("BSCI207",catalog,rules)
    comm=[("COMM107",rules["current_sections"]["COMM107"],normalized_current_info("COMM107",rules))]
    fixed=[x for x in base if x[0] in ("ENES210","INST155")]
    for a,b,c,d in itertools.product(ch231,ch232,b207,comm):
        sch=[a,b,c,d]+fixed
        if valid_schedule(sch,rules):candidates.append(("section",sch))

    # 2) Approved easier-than-Sefton FSOC alternatives replacing COMM107.
    fsoc=[]
    for fc in ["COMM107","COMM107C","COMM200","INAG110","JOUR130","THET285"]:
        for sec,info in catalog.get(fc,{}).items():
            if info.get("open",0)>0 and fsoc_allowed(info,rules):
                fsoc.append((fc,sec,info))
    for a,b,c,d in itertools.product(ch231,ch232,b207,fsoc):
        sch=[a,b,c,d]+fixed
        if valid_schedule(sch,rules):candidates.append(("fsoc",sch))

    # 3) BSCI331 / BIOM301 can replace COMM107 or BSCI207, but still exactly 16.
    academics=[]
    for ac in ["BSCI331","BIOM301"]:
        for sec,info in catalog.get(ac,{}).items():
            if info.get("open",0)>0:academics.append((ac,sec,info))
    for ac in academics:
        # replace COMM107; retain BSCI207
        for a,b,c in itertools.product(ch231,ch232,b207):
            sch=[a,b,c,ac]+fixed
            if valid_schedule(sch,rules):candidates.append(("academic",sch))
        # replace BSCI207; retain current COMM107
        for a,b in itertools.product(ch231,ch232):
            sch=[a,b,comm[0],ac]+fixed
            if valid_schedule(sch,rules):candidates.append(("academic",sch))

    if not candidates:return None,base_score,None
    best=max(candidates,key=lambda x:schedule_score(x[1],rules))
    return best[1],base_score,best[0]

def describe_change(best,rules):
    current=rules.get("current_sections",{})
    new={c:(s,i) for c,s,i in best}
    removed=[c for c in current if c not in new]
    added=[c for c in new if c not in current]
    changed=[c for c in current if c in new and new[c][0]!=current[c]]
    bits=[]
    if added:bits.append("Add "+", ".join(f"{c}-{new[c][0]}" for c in added))
    if removed:bits.append("Drop "+", ".join(removed))
    if changed:bits.append("Switch "+", ".join(f"{c} to {new[c][0]}" for c in changed))
    return "; ".join(bits) or "No change"

def format_schedule(best):
    lines=[]
    for c,s,i in sorted(best,key=lambda x:x[0]):
        mts=i.get("meetings",[])
        mt="; ".join(m.get("label","") for m in mts) if mts else "no fixed meeting"
        lines.append(f"{c}-{s} | {i.get('instructor','unknown')} | {mt}")
    return "\n".join(lines)

def newly_open_course_section(course,sec,best):
    return any(c==course and s==sec for c,s,_ in best)

def threshold_for(kind,best,rules):
    cfg=rules.get("schedule_score",{})
    if kind=="academic":return float(cfg.get("academic_swap_min",5))
    if kind=="fsoc":return float(cfg.get("fsoc_swap_min",2))
    # If the best schedule leaves Stocker, require a much larger improvement.
    for c,s,i in best:
        if c=="CHEM231":
            pref=rules.get("preferred_instructors",{}).get("CHEM231","").lower()
            if pref and pref not in i.get("instructor","").lower():
                return float(cfg.get("nonstocker_chem231_min",8))
    return float(cfg.get("section_improvement_min",2))

def send_ntfy(course,section,info,reason,best):
    if not NTFY_TOPIC:raise RuntimeError("NTFY_TOPIC missing")
    official="; ".join(m.get("label","") for m in info.get("meetings",[])) or "no fixed meeting"
    msg=(f"BETTER 16-CREDIT OPTION\n"
         f"Opened: {course}-{section}\n"
         f"Professor: {info.get('instructor','unknown')}\n"
         f"Official time: {official}\n"
         f"Why: {reason}\n\n"
         f"Recommended schedule:\n{format_schedule(best)}")
    r=requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",data=msg.encode(),
        headers={"Title":f"Better schedule: {course} {section}","Priority":"high",
                 "Tags":"school","Content-Type":"text/plain; charset=utf-8"},
        timeout=REQUEST_TIMEOUT);r.raise_for_status()

def sections_to_check(item,found):
    wanted=[str(x).upper() for x in item.get("sections",["*"])]
    excluded={str(x).upper() for x in item.get("exclude_sections",[])}
    chosen=sorted(found) if "*" in wanted else wanted
    return [s for s in chosen if s not in excluded and s in found]

def recommendation_signature(best):
    if not best:
        return None
    # Stable identity of the recommended complete schedule.
    return "|".join(sorted(f"{c}-{s}" for c,s,_ in best))

def scan_all(config,state,rules,meta,baseline=False):
    changed=False;catalog={}
    for i,item in enumerate(config["watch"]):
        course=item["course"].upper()
        _,found=parse_page(get_page(course_url(course)),course)
        # Optimizer may use only sections explicitly allowed by config.json.
        allowed=sections_to_check(item,found)
        found={sec:found[sec] for sec in allowed}
        catalog[course]=found
        if not found:print(f"WARNING: no allowed sections parsed for {course}",file=sys.stderr)
        if i<len(config["watch"])-1:time.sleep(random.uniform(3,5))

    best,base_score,kind=best_schedule(catalog,rules)
    best_score=schedule_score(best,rules) if best else float("-inf")
    improvement=best_score-base_score if best else float("-inf")
    threshold=threshold_for(kind,best,rules) if best else float("inf")
    qualifies=best is not None and improvement>=threshold
    signature=recommendation_signature(best) if qualifies else None
    previous_signature=meta.get("last_actionable_recommendation")

    print(f"Best live schedule improvement: {improvement:.2f}; threshold={threshold:.2f}; kind={kind}")
    if best:print(describe_change(best,rules))

    # Keep seat-state history for diagnostics, but notification no longer depends on
    # CLOSED -> OPEN. This means the user does not have to manually check Testudo.
    for item in config["watch"]:
        course=item["course"].upper();found=catalog.get(course,{})
        for sec in sections_to_check(item,found):
            key=f"{course}-{sec}";open_now=found[sec]["open"]>0;old=state.get(key)
            if old is None:
                print(f"BASELINE {key}: {'OPEN' if open_now else 'CLOSED'}")
                state[key]=open_now;changed=True
            elif bool(old)!=open_now:
                print(f"STATE {key}: {'OPEN' if open_now else 'CLOSED'}")
                state[key]=open_now;changed=True

    if qualifies:
        if signature != previous_signature:
            # Alert on the best actionable schedule even if its key seat was already
            # open at baseline. Suppress repeats until the recommendation changes.
            newmap={c:(s,i) for c,s,i in best}
            current=rules.get("current_sections",{})
            trigger=None
            for c,(s,i) in newmap.items():
                if c not in current or current.get(c)!=s:
                    trigger=(c,s,i);break
            if trigger is None:
                trigger=best[0]
            c,s,i=trigger
            reason=f"{describe_change(best,rules)}; score improvement {improvement:.2f}"
            if not baseline:
                send_ntfy(c,s,i,reason,best)
                print(f"ALERT recommendation changed: {signature}")
            else:
                print(f"BASELINE actionable recommendation recorded without alert: {signature}")
            meta["last_actionable_recommendation"]=signature
        else:
            print("Actionable recommendation unchanged. No repeat ntfy.")
    else:
        if previous_signature:
            print("No qualifying improvement now; clearing prior recommendation so a future return can alert.")
        meta.pop("last_actionable_recommendation",None)

    return changed

def due_now(now,meta):
    md=(now.month,now.day);daytime=7<=now.hour<23
    if md>(9,14):return False,None
    if (8,28)<=md<=(8,30):mins=10 if daytime else 30
    elif (8,31)<=md<=(9,4):mins=5 if daytime else 15
    elif (9,5)<=md<=(9,13):mins=10 if daytime else 30
    elif md==(9,14):mins=5 if daytime else 15
    else:mins=30
    last=meta.get("last_check_epoch")
    return (last is None or now.timestamp()-float(last)>=mins*60-30),mins

def main():
    rules=load_json(RULES_PATH,{})
    tz=ZoneInfo(rules.get("timezone","America/New_York"))
    now=datetime.now(tz);config=load_json(CONFIG_PATH,{})
    state=load_json(STATE_PATH,{});meta=load_json(META_PATH,{})
    due,interval=due_now(now,meta)
    force_full_scan=os.getenv("FORCE_FULL_SCAN","false").strip().lower() in ("1","true","yes","on")
    print(f"Local time: {now.isoformat()}");print(f"Target interval: {interval} min")
    if not due and not force_full_scan:
        print("Not due. Exiting without contacting Testudo.");return 0
    if force_full_scan and not due:
        print("FORCE_FULL_SCAN enabled. Bypassing due-time check.")
    if now.timestamp()<float(meta.get("blocked_until_epoch",0)):
        print("Conservative pause still active. Exiting.");return 0
    sentinels=[c.upper() for c in config.get("sentinel_courses",
                 [config.get("sentinel_course","BSCI331")])]
    try:
        current_stamps={}
        for idx,sentinel in enumerate(sentinels):
            stamp,_=parse_page(get_page(course_url(sentinel)),sentinel)
            current_stamps[sentinel]=stamp
            print(f"Sentinel {sentinel} snapshot: {stamp}")
            if idx<len(sentinels)-1:
                time.sleep(random.uniform(2,4))

        last_stamps=meta.get("last_snapshots",{})
        if not isinstance(last_stamps,dict):
            last_stamps={}
        old_single=meta.get("last_snapshot")
        if old_single and not last_stamps and sentinels:
            last_stamps[sentinels[0]]=old_single

        first=not bool(state)
        changed_sentinels=[
            c for c,stamp in current_stamps.items()
            if stamp and last_stamps.get(c) and stamp!=last_stamps.get(c)
        ]
        new_known_sentinels=[
            c for c,stamp in current_stamps.items()
            if stamp and not last_stamps.get(c)
        ]

        if first:
            print("First run: establishing baseline.")
            scan_all(config,state,rules,meta,baseline=True);save_json(STATE_PATH,state)
        elif force_full_scan:
            print("FORCE_FULL_SCAN enabled. Running full optimizer scan now.")
            if scan_all(config,state,rules,meta,baseline=False):save_json(STATE_PATH,state)
        elif changed_sentinels:
            print("Snapshot changed on: "+", ".join(changed_sentinels)+". Running smart full scan.")
            if scan_all(config,state,rules,meta,baseline=False):save_json(STATE_PATH,state)
        elif new_known_sentinels:
            print("New sentinel timestamp baseline recorded for: "+", ".join(new_known_sentinels))
        elif all(stamp is None for stamp in current_stamps.values()):
            last_full=float(meta.get("last_full_scan_epoch",0))
            if now.timestamp()-last_full>=3600:
                print("No sentinel timestamp found; conservative fallback full scan.")
                if scan_all(config,state,rules,meta,baseline=False):save_json(STATE_PATH,state)
                meta["last_full_scan_epoch"]=now.timestamp()
        else:
            print("All sentinel snapshots unchanged. No full scan.")

        meta["last_snapshots"]={c:s for c,s in current_stamps.items() if s}
        meta.pop("last_snapshot",None)
        meta["last_check_epoch"]=now.timestamp();meta.pop("blocked_until_epoch",None)
        save_json(META_PATH,meta);return 0
    except RuntimeError as e:
        txt=str(e);print(f"ERROR: {txt}",file=sys.stderr)
        meta["last_check_epoch"]=now.timestamp()
        meta["blocked_until_epoch"]=now.timestamp()+(7200 if txt.startswith("ACCESS_CONTROL_") else 1800)
        save_json(META_PATH,meta);return 0
    except Exception as e:
        print(f"ERROR: {e}",file=sys.stderr)
        meta["last_check_epoch"]=now.timestamp();meta["blocked_until_epoch"]=now.timestamp()+1800
        save_json(META_PATH,meta);return 0

if __name__=="__main__":raise SystemExit(main())
