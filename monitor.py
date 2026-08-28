#!/usr/bin/env python3
import json, os, random, re, sys, time
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
    # ask caches for a current representation without increasing request frequency
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
    # generic token parser, longest first
    out=[]; i=0
    while i<len(tok):
        if tok.startswith("Tu",i): out.append("Tu"); i+=2
        elif tok.startswith("Th",i): out.append("Th"); i+=2
        elif tok[i] in "MWF": out.append(tok[i]); i+=1
        else: i+=1
    return out

def scope_course_text(text,course):
    pat=re.compile(rf"(?<![A-Za-z0-9]){re.escape(course)}(?![A-Za-z0-9]).{{0,250}}?Syllabus Repository",re.I|re.S)
    sm=pat.search(text)
    if not sm:return text
    start=sm.start(); end=len(text)
    next_course=re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,5}\d{3,4}[A-Z]?)(?![A-Za-z0-9]).{0,250}?Syllabus Repository",re.I|re.S)
    nm=next_course.search(text,sm.end())
    if nm:end=nm.start()
    return text[start:end]

def parse_page(html,course):
    soup=BeautifulSoup(html,"html.parser")
    text=soup.get_text(" ",strip=True)
    m=re.search(r"Open Seats as of\s+(\d{1,2}/\d{1,2}/\d{4}\s+at\s+\d{1,2}:\d{2}\s+[AP]M)",text,re.I)
    stamp=" ".join(m.group(1).split()) if m else None
    scoped=scope_course_text(text,course)

    # Parse only genuine Testudo section headers:
    #   0301 *  Phillip Moses  Seats (Total: 19, Open: 0, ...)
    # Testudo may place one or more restriction asterisks after the section ID.
    # Instructor text is deliberately restricted to name-like characters.
    # This prevents meeting-time fragments such as "30PM", "TUTH", or "HERE"
    # from being mistaken for section IDs while still supporting IDs such as
    # 0301, ESG1, FC05, etc.
    sr=re.compile(
      r"(?<![A-Za-z0-9])"
      r"([A-Za-z0-9]{4})"
      r"(?![A-Za-z0-9])"
      r"\s*(?:\*+\s*)?"
      r"([A-Za-zÀ-ÖØ-öø-ÿ.'’\-]+(?:\s+[A-Za-zÀ-ÖØ-öø-ÿ.'’\-]+){0,7})"
      r"\s+Seats\s*\(\s*Total:\s*\d+\s*,\s*Open:\s*(\d+)",
      re.I|re.S)
    starts=list(sr.finditer(scoped)); out={}
    time_re=re.compile(r"\b(MWF|TuTh|MW|MF|WF|MTh|MTu|WTh|ThF|M|Tu|W|Th|F)\s+"
                       r"(\d{1,2}:\d{2}(?:am|pm))\s*-\s*(\d{1,2}:\d{2}(?:am|pm))",re.I)
    for i,sm in enumerate(starts):
        sec=sm.group(1).upper(); instr=" ".join(sm.group(2).split()); open_count=int(sm.group(3))
        end=starts[i+1].start() if i+1<len(starts) else len(scoped)
        seg=scoped[sm.end():end]
        meetings=[]
        for tm in time_re.finditer(seg):
            days=expand_days(tm.group(1))
            st,en=parse_clock(tm.group(2)),parse_clock(tm.group(3))
            if st is not None and en is not None:
                meetings.append({"days":days,"start":st,"end":en,
                                 "label":f"{tm.group(1)} {tm.group(2)}-{tm.group(3)}"})
        # first occurrence wins if a duplicate somehow survives scoping
        out.setdefault(sec,{"open":open_count,"instructor":instr,"meetings":meetings})
    return stamp,out

def overlaps(a,b):
    if not set(a["days"])&set(b["days"]):return False
    return a["start"]<b["end"] and b["start"]<a["end"]

def group_for(course,rules): return rules.get("course_groups",{}).get(course,course)
def priority_for(course,rules): return rules.get("priority",{}).get(group_for(course,rules),0)

def physically_feasible(info,rules):
    earliest=hm_to_min(rules.get("earliest_start","09:30"))
    latest=hm_to_min(rules.get("latest_end","19:15"))
    for mt in info.get("meetings",[]):
        if mt["start"]<earliest or mt["end"]>latest:
            return False
    return True

def current_conflicts_for(info,rules,skip_courses=None):
    skip=set(skip_courses or [])
    conflicts=[]
    meetings=info.get("meetings",[])
    for cur,curmts in rules.get("current_meetings",{}).items():
        if cur in skip:
            continue
        for cmt in curmts:
            cm={"days":cmt["days"],"start":hm_to_min(cmt["start"]),"end":hm_to_min(cmt["end"])}
            if any(overlaps(mt,cm) for mt in meetings):
                conflicts.append(cur)
                break
    return sorted(set(conflicts))

def meetings_conflict(info_a,info_b):
    return any(overlaps(a,b) for a in info_a.get("meetings",[]) for b in info_b.get("meetings",[]))

def nonpreferred_chem231_value(sec,info,rules,catalog):
    """
    Decide whether leaving Stocker is justified by live, currently-open options.

    A non-Stocker CHEM231 section is worth alerting only if it *actually unlocks*
    an open course that Stocker's current CHEM231 meetings block, or a strong
    two-course combination. Merely moving CHEM232/COMM107/ENES210 is not enough.
    """
    preferred=rules.get("preferred_instructors",{}).get("CHEM231","").strip().lower()
    instructor=info.get("instructor","").strip().lower()
    if not preferred or preferred in instructor:
        return True,None

    current_chem={"meetings":[]}
    for mt in rules.get("current_meetings",{}).get("CHEM231",[]):
        current_chem["meetings"].append({
            "days":mt["days"],
            "start":hm_to_min(mt["start"]),
            "end":hm_to_min(mt["end"])
        })

    single_min=int(rules.get("nonpreferred_chem231_single_unlock_min_priority",75))
    combo_min=int(rules.get("nonpreferred_chem231_combo_min_priority",60))
    combo_total=int(rules.get("nonpreferred_chem231_combo_total_priority",130))
    excluded=set(rules.get("nonpreferred_chem231_exclude_unlock_courses",
                           ["CHEM231","CHEM232","BSCI207"]))

    candidates=[]
    movable=set(rules.get("replaceable_current",[]))

    for target_course,sections in catalog.items():
        if target_course in excluded:
            continue
        tp=priority_for(target_course,rules)
        if tp<combo_min:
            continue

        for target_sec,target_info in sections.items():
            if target_info.get("open",0)<=0:
                continue
            if not physically_feasible(target_info,rules):
                continue

            # This course must be something Stocker blocks but the new CHEM231 does not.
            if not meetings_conflict(target_info,current_chem):
                continue
            if meetings_conflict(target_info,info):
                continue

            # It must also be able to fit after moving only lower-priority,
            # explicitly replaceable current courses.
            conflicts=current_conflicts_for(target_info,rules,skip_courses={"CHEM231",target_course})
            if any(c not in movable for c in conflicts):
                continue
            if any(priority_for(c,rules)>=tp for c in conflicts):
                continue

            candidates.append((tp,target_course,target_sec,target_info,conflicts))

    # One major live unlock is enough.
    major=[x for x in candidates if x[0]>=single_min]
    if major:
        major.sort(reverse=True,key=lambda x:x[0])
        tp,c,s,_,conf=major[0]
        extra=f"; {','.join(conf)} would move" if conf else ""
        return True,f"worth changing CHEM231 instructor: unlocks open {c}-{s}{extra}"

    # Or two meaningful live unlocks that can coexist with each other.
    for i,a in enumerate(candidates):
        for b in candidates[i+1:]:
            if a[1]==b[1]:
                continue
            if a[0]+b[0] < combo_total:
                continue
            if meetings_conflict(a[3],b[3]):
                continue
            return True,(f"worth changing CHEM231 instructor: unlocks open "
                         f"{a[1]}-{a[2]} + {b[1]}-{b[2]}")

    return False,(f"non-preferred CHEM231 instructor ({info.get('instructor','unknown')}); "
                  "no sufficiently valuable open course combination is unlocked")

def actionability(course,sec,info,rules,catalog=None):
    earliest=hm_to_min(rules.get("earliest_start","09:30"))
    latest=hm_to_min(rules.get("latest_end","19:15"))
    meetings=info.get("meetings",[])

    # For CHEM231, Stocker remains the default. Another instructor is surfaced
    # only when this live scan shows that switching actually unlocks enough value.
    if course=="CHEM231" and catalog is not None:
        worth,reason=nonpreferred_chem231_value(sec,info,rules,catalog)
        if not worth:
            return False,reason
        if reason:
            chem231_override_reason=reason
        else:
            chem231_override_reason=None
    else:
        chem231_override_reason=None

    # Online/no fixed meeting is physically feasible.
    for mt in meetings:
        if mt["start"]<earliest:
            return False,f"starts too early ({mt['label']})"
        if mt["end"]>latest:
            return False,f"ends too late ({mt['label']})"

    conflicts=[]
    for cur,curmts in rules.get("current_meetings",{}).items():
        # if this is an alternate of the same current course, replacing it is allowed
        if cur==course: continue
        for cmt in curmts:
            cm={"days":cmt["days"],"start":hm_to_min(cmt["start"]),"end":hm_to_min(cmt["end"])}
            if any(overlaps(mt,cm) for mt in meetings):
                conflicts.append(cur); break

    conflicts=sorted(set(conflicts))
    if not conflicts:
        return True,chem231_override_reason or "fits current schedule"

    movable=set(rules.get("replaceable_current",[]))
    immovable=[c for c in conflicts if c not in movable]
    if immovable:
        return False,"conflicts with non-movable "+",".join(immovable)

    # Same-requirement substitution (e.g. dual SCIS+DSSP can replace ENES210)
    grp=group_for(course,rules)
    same=set(rules.get("same_slot_replacements",{}).get(grp,[]))
    if set(conflicts).issubset(same) and conflicts:
        return True,chem231_override_reason or ("can replace "+",".join(conflicts))

    candp=priority_for(course,rules)
    conflictp=max((priority_for(c,rules) for c in conflicts),default=0)
    if candp>conflictp:
        return True,chem231_override_reason or ("higher-priority option if "+",".join(conflicts)+" moves")

    # Alternate section of an already held course is still potentially useful.
    if course in rules.get("current_sections",{}):
        return True,chem231_override_reason or "alternate section may unlock a better combination"

    return False,"only works by displacing equal/higher-priority "+",".join(conflicts)

def send_ntfy(course,section,reason):
    if not NTFY_TOPIC: raise RuntimeError("NTFY_TOPIC missing")
    msg=f"SEAT AVAILABLE\n{reason}"
    r=requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",data=msg.encode(),
      headers={"Title":f"{course} {section}","Priority":"high","Tags":"school"},
      timeout=REQUEST_TIMEOUT); r.raise_for_status()

def sections_to_check(item,found):
    wanted=[str(x).upper() for x in item.get("sections",["*"])]
    excluded={str(x).upper() for x in item.get("exclude_sections",[])}
    chosen=sorted(found) if "*" in wanted else wanted
    return [s for s in chosen if s not in excluded and s in found]

def scan_all(config,state,rules,baseline=False):
    changed=False
    catalog={}

    # First pass: fetch and parse every watched course exactly once.
    # This does not increase the number of Testudo requests; it only lets the
    # actionability logic see all currently-open sections before deciding alerts.
    for i,item in enumerate(config["watch"]):
        course=item["course"].upper()
        _,found=parse_page(get_page(course_url(course)),course)
        if not found:
            print(f"WARNING: no sections parsed for {course}",file=sys.stderr)
        catalog[course]=found
        if i<len(config["watch"])-1:
            time.sleep(random.uniform(3,5))

    # Second pass: evaluate transitions using the complete live catalog.
    for item in config["watch"]:
        course=item["course"].upper()
        found=catalog.get(course,{})
        if not found:
            continue
        for sec in sections_to_check(item,found):
            key=f"{course}-{sec}"; info=found[sec]; open_now=info["open"]>0
            old=state.get(key)
            if old is None:
                ok,why=actionability(course,sec,info,rules,catalog)
                print(f"BASELINE {key}: {'OPEN' if open_now else 'CLOSED'} | actionable={ok} | {why}")
                state[key]=open_now; changed=True
            elif bool(old)!=open_now:
                if open_now:
                    ok,why=actionability(course,sec,info,rules,catalog)
                    if ok and not baseline:
                        send_ntfy(course,sec,why)
                        print(f"ALERT {key}: OPEN | {why}")
                    else:
                        print(f"SUPPRESSED {key}: OPEN | {why}")
                else:
                    print(f"STATE {key}: CLOSED")
                state[key]=open_now; changed=True
    return changed

def due_now(now,meta):
    md=(now.month,now.day); daytime=7<=now.hour<23
    if md>(9,14): return False,None
    if (8,28)<=md<=(8,30): mins=10 if daytime else 30
    elif (8,31)<=md<=(9,4): mins=5 if daytime else 15
    elif (9,5)<=md<=(9,13): mins=10 if daytime else 30
    elif md==(9,14): mins=5 if daytime else 15
    else: mins=30
    last=meta.get("last_check_epoch")
    return (last is None or now.timestamp()-float(last)>=mins*60-30),mins

def main():
    rules=load_json(RULES_PATH,{})
    tz=ZoneInfo(rules.get("timezone","America/New_York"))
    now=datetime.now(tz); config=load_json(CONFIG_PATH,{})
    state=load_json(STATE_PATH,{}); meta=load_json(META_PATH,{})
    due,interval=due_now(now,meta)
    print(f"Local time: {now.isoformat()}"); print(f"Target interval: {interval} min")
    if not due: print("Not due. Exiting without contacting Testudo."); return 0
    if now.timestamp()<float(meta.get("blocked_until_epoch",0)):
        print("Conservative pause still active. Exiting."); return 0
    sentinel=config.get("sentinel_course","BSCI222").upper()
    try:
        stamp,_=parse_page(get_page(course_url(sentinel)),sentinel)
        print(f"Sentinel snapshot: {stamp}")
        last_stamp=meta.get("last_snapshot"); first=not bool(state)
        if first:
            print("First run: establishing baseline.")
            scan_all(config,state,rules,baseline=True); save_json(STATE_PATH,state)
        elif stamp and last_stamp and stamp!=last_stamp:
            print("Snapshot changed. Running smart full scan.")
            if scan_all(config,state,rules,baseline=False): save_json(STATE_PATH,state)
        elif stamp is None:
            last_full=float(meta.get("last_full_scan_epoch",0))
            if now.timestamp()-last_full>=3600:
                print("No timestamp found; conservative fallback full scan.")
                if scan_all(config,state,rules,baseline=False): save_json(STATE_PATH,state)
                meta["last_full_scan_epoch"]=now.timestamp()
        else: print("Snapshot unchanged. No full scan.")
        if stamp: meta["last_snapshot"]=stamp
        meta["last_check_epoch"]=now.timestamp(); meta.pop("blocked_until_epoch",None)
        save_json(META_PATH,meta); return 0
    except RuntimeError as e:
        txt=str(e); print(f"ERROR: {txt}",file=sys.stderr)
        meta["last_check_epoch"]=now.timestamp()
        meta["blocked_until_epoch"]=now.timestamp()+(7200 if txt.startswith("ACCESS_CONTROL_") else 1800)
        save_json(META_PATH,meta); return 0
    except Exception as e:
        print(f"ERROR: {e}",file=sys.stderr)
        meta["last_check_epoch"]=now.timestamp(); meta["blocked_until_epoch"]=now.timestamp()+1800
        save_json(META_PATH,meta); return 0

if __name__=="__main__": raise SystemExit(main())
