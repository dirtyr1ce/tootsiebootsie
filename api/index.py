"""
TootsieBootsie API v3 — Discovery Engine + Traces Social Layer
"""

from fastapi import FastAPI, HTTPException, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
import httpx, os, json, hashlib, time, asyncio, uuid
from datetime import datetime, date
from typing import Optional

app = FastAPI(title="TootsieBootsie API", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

GOOGLE_KEY     = os.getenv("GOOGLE_KEY", "")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_KEY", "")
EVENTBRITE_KEY = os.getenv("EVENTBRITE_KEY", "")
HIKING_KEY     = os.getenv("HIKING_KEY", "200897064-4b73f1b3b7e2a3c4f9c9e2e4c7a7e5e5")
SUPABASE_URL   = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY   = os.getenv("SUPABASE_ANON_KEY", "")

_cache: dict = {}
def cache_get(k, ttl=3600):
    e = _cache.get(k)
    return e["data"] if e and time.time()-e["ts"] < ttl else None
def cache_set(k, data): _cache[k] = {"data":data,"ts":time.time()}
def ck(*a): return hashlib.md5("|".join(str(x) for x in a).encode()).hexdigest()

def sb_headers(jwt=None):
    h = {"apikey":SUPABASE_KEY,"Content-Type":"application/json",
         "Authorization":f"Bearer {jwt or SUPABASE_KEY}"}
    return h

async def sb_get(path, params=None, jwt=None):
    if not SUPABASE_URL: return None
    async with httpx.AsyncClient(timeout=8) as c:
        r = await c.get(f"{SUPABASE_URL}/rest/v1/{path}",
                        headers=sb_headers(jwt), params=params or {})
        return r.json() if r.status_code < 300 else None

async def sb_post(path, data, jwt=None):
    if not SUPABASE_URL: return None
    async with httpx.AsyncClient(timeout=8) as c:
        r = await c.post(f"{SUPABASE_URL}/rest/v1/{path}",
                         headers={**sb_headers(jwt),"Prefer":"return=representation"},
                         json=data)
        return r.json() if r.status_code < 300 else None

async def sb_upload(bucket, path, data, content_type, jwt):
    if not SUPABASE_URL: return None
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{SUPABASE_URL}/storage/v1/object/{bucket}/{path}",
                         headers={"apikey":SUPABASE_KEY,"Authorization":f"Bearer {jwt}",
                                  "Content-Type":content_type,"x-upsert":"true"},
                         content=data)
        if r.status_code < 300:
            return f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}"
        return None

class DiscoverRequest(BaseModel):
    lat: float; lng: float; city: str; state: str = ""
    mode: str = "local"; radius: int = 60
    travel_date: str = ""; count: int = 6

class TraceCreate(BaseModel):
    place_name: str; place_type: str = "local"
    lat: float; lng: float; sentence: str
    day_story_id: Optional[str] = None

    @field_validator("sentence")
    def check_len(cls, v):
        if len(v) > 120: raise ValueError("Max 120 chars")
        return v

class DayStoryCreate(BaseModel):
    city: str; travel_date: str
    title: str = ""; trace_ids: list = []

@app.get("/api/health")
def health():
    return {"status":"ok","google":bool(GOOGLE_KEY),
            "anthropic":bool(ANTHROPIC_KEY),"supabase":bool(SUPABASE_URL)}

@app.get("/api/geocode")
async def geocode(lat: float, lng: float):
    k = ck("geocode",round(lat,2),round(lng,2))
    if c := cache_get(k): return c
    async with httpx.AsyncClient(timeout=6) as cl:
        r = await cl.get("https://nominatim.openstreetmap.org/reverse",
            params={"lat":lat,"lon":lng,"format":"json","zoom":10},
            headers={"User-Agent":"TootsieBootsie/3.0"})
        a = r.json().get("address",{})
    result = {"city":a.get("city") or a.get("town") or a.get("village") or "Your City",
              "state":a.get("state",""),"country":a.get("country_code","").upper()}
    cache_set(k,result); return result

@app.post("/api/discover")
async def discover(req: DiscoverRequest):
    travel_date = req.travel_date or date.today().isoformat()
    k = ck("disc3",round(req.lat,2),round(req.lng,2),req.mode,req.radius,travel_date)
    if c := cache_get(k,1800): return c

    results = await asyncio.gather(
        _claude_discover(req, travel_date),
        _events(req.lat,req.lng,travel_date) if req.mode=="local" else asyncio.sleep(0),
        _google_places(req.lat,req.lng,req.mode) if GOOGLE_KEY else asyncio.sleep(0),
        _nearby_traces(req.lat,req.lng),
        return_exceptions=True
    )
    claude_r,events_r,google_r,traces_r = [r if not isinstance(r,Exception) else [] for r in results]
    discoveries = _merge(claude_r or [],events_r or [],google_r or [],req)
    if traces_r:
        for d in discoveries:
            nm = d.get("name","").lower()
            d["traces"] = [t for t in traces_r if nm[:6] in t.get("place_name","").lower()][:4]
    result = {"places":discoveries,"city":req.city,"date":travel_date,
              "mode":req.mode,"recent_traces":traces_r[:12]}
    cache_set(k,result); return result

async def _claude_discover(req, travel_date):
    if not ANTHROPIC_KEY: return []
    day = datetime.fromisoformat(travel_date).strftime("%A, %B %d")
    mode_ctx = {"local":f"discoveries within {req.city}",
                "trip":f"towns within {req.radius}-min drive of {req.city}",
                "hike":f"hiking trails near {req.city}"}[req.mode]
    prompt = f"""City: {req.city}{', '+req.state if req.state else ''}
Date: {day} | {req.lat:.4f},{req.lng:.4f} | Mode: {mode_ctx}
Generate {req.count} genuinely LOCAL discoveries. Nothing generic.
Return ONLY JSON array: [{{"id":int,"name":"str","emoji":"str","type":"{req.mode}",
"why":"2-3 specific sentences","tip":"one insider tip","tags":["T1","T2","T3"],
"buzz":float,"dist":"str","driveMin":int,"isEvent":bool,"eventTime":"str"}}]"""
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","Content-Type":"application/json"},
                json={"model":"claude-sonnet-4-20250514","max_tokens":2500,
                      "system":"Return ONLY valid JSON array. No markdown.",
                      "messages":[{"role":"user","content":prompt}]})
            raw = r.json().get("content",[{}])[0].get("text","[]")
            return json.loads(raw.replace("```json","").replace("```","").strip())
    except Exception as e: print(f"Claude: {e}"); return []

async def _events(lat,lng,travel_date):
    if not EVENTBRITE_KEY: return []
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("https://www.eventbriteapi.com/v3/events/search/",
                headers={"Authorization":f"Bearer {EVENTBRITE_KEY}"},
                params={"location.latitude":lat,"location.longitude":lng,
                        "location.within":"20mi","start_date.range_start":f"{travel_date}T00:00:00",
                        "start_date.range_end":f"{travel_date}T23:59:00",
                        "expand":"venue","sort_by":"best","page_size":3})
            return [{"id":hash(e["id"])%10000,"name":e.get("name",{}).get("text",""),
                "emoji":"🎭","type":"local","why":e.get("description",{}).get("text","")[:200] or "Local event today.",
                "tip":"Book ahead — check website for times.","tags":["Event","Today"],
                "buzz":0.78,"dist":"Nearby","driveMin":0,"isEvent":True,
                "url":e.get("url","")} for e in r.json().get("events",[])]
    except: return []

async def _google_places(lat,lng,mode):
    if not GOOGLE_KEY: return []
    types = {"local":["tourist_attraction","cafe"],"trip":["tourist_attraction"],"hike":["park"]}
    results = []
    async with httpx.AsyncClient(timeout=10) as c:
        for pt in types.get(mode,["tourist_attraction"])[:2]:
            try:
                r = await c.get("https://maps.googleapis.com/maps/api/place/nearbysearch/json",
                    params={"location":f"{lat},{lng}","radius":5000 if mode=="local" else 50000,
                            "type":pt,"key":GOOGLE_KEY})
                for p in r.json().get("results",[])[:5]:
                    photo_url = None
                    if p.get("photos"):
                        ref = p["photos"][0]["photo_reference"]
                        photo_url = f"https://maps.googleapis.com/maps/api/place/photo?maxwidth=800&photo_reference={ref}&key={GOOGLE_KEY}"
                    results.append({"google_name":p["name"],"rating":p.get("rating",0),
                        "photo_url":photo_url,"open_now":p.get("opening_hours",{}).get("open_now"),
                        "vicinity":p.get("vicinity",""),"place_id":p.get("place_id","")})
            except: pass
    return results

async def _nearby_traces(lat,lng,limit=20):
    if not SUPABASE_URL: return []
    try:
        deg = 0.45
        data = await sb_get("traces_with_user",
            {"select":"*","lat":f"gte.{lat-deg}","lng":f"gte.{lng-deg}",
             "order":"created_at.desc","limit":limit})
        return data or []
    except: return []

def _merge(claude,events,google,req):
    out = list(events[:2])
    for i,p in enumerate(claude):
        if not isinstance(p,dict): continue
        e = dict(p); e.setdefault("id",1000+i); e["source"]="claude"; e["traces"]=[]
        match = next((g for g in google
                      if set(p.get("name","").lower().split()) &
                         set(g.get("google_name","").lower().split())),None)
        if match:
            e["photo_url"]=match.get("photo_url")
            e["rating"]=match.get("rating") or p.get("rating",4.5)
            e["open_now"]=match.get("open_now")
            e["address"]=match.get("vicinity","")
        out.append(e)
    return out[:8]

@app.get("/api/trails")
async def trails(lat: float, lng: float, max_distance: int = 30):
    k = ck("trails",round(lat,2),round(lng,2))
    if c := cache_get(k): return c
    DIFF={"green":1,"greenBlue":2,"blue":2,"blueBlack":3,"black":4,"dblack":5}
    ICON={"green":"🌿","greenBlue":"🌲","blue":"🌲","blueBlack":"⛰️","black":"⛰️","dblack":"🏔️"}
    POS=[[-3.5,.8,0],[0,.8,-2.5],[3.5,.8,0]]
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://www.hikingproject.com/data/get-trails",
                params={"lat":lat,"lon":lng,"maxDistance":max_distance,"key":HIKING_KEY})
            tl = r.json().get("trails",[])
        result=[]
        for i,t in enumerate(tl[:3]):
            mi=t.get("length",4); asc=t.get("ascent",500); hrs=mi/2+asc/1000
            diff=t.get("difficulty","blue"); easy=diff in("green","greenBlue")
            sats=[{"icon":"🌅","label":"Pre-hike breakfast","name":"Trailhead Café","desc":"Fuel up before hitting the trail.","buzz":.76,"rating":4.4},
                  {"icon":ICON.get(diff,"🌲"),"label":"Key viewpoint","name":t["name"]+(" Overlook" if easy else " Summit"),"desc":t.get("summary") or "The main highlight.","buzz":.94,"rating":4.8},
                  {"icon":"🍺","label":"Post-hike drinks","name":"The Recovery Bar","desc":"Cold drinks after the trail.","buzz":.84,"rating":4.6},
                  {"icon":"🌙","label":"Recovery dinner","name":"Hiker's Table","desc":"Big portions, well earned.","buzz":.79,"rating":4.4}]
            if easy: sats.insert(3,{"icon":"🌆","label":"Afternoon free!","name":"Explore Town","desc":"Half-day = afternoon open.","buzz":.70,"rating":4.5})
            result.append({"id":200+i,"name":t["name"],"emoji":ICON.get(diff,"🌲"),"type":"hike",
                "why":t.get("summary") or "A beautiful local trail.","tip":"Check conditions before you go.",
                "tags":["Trail",diff.title(),"Nature"],"dist":f"{mi:.1f} mi","driveMin":0,
                "buzz":min(.99,.5+t.get("stars",3)/10),"rating":t.get("stars",4.0),
                "pos":POS[i],"url":t.get("url",""),
                "trail":{"d":f"{mi:.1f} mi","e":f"+{int(asc):,} ft","diff":DIFF.get(diff,2)},
                "source":"hiking_project","traces":[]})
        cache_set(k,result); return result
    except Exception as e: print(f"Trails: {e}"); return []

@app.post("/api/day-trips")
async def day_trips(req: DiscoverRequest):
    k = ck("dt",round(req.lat,2),round(req.lng,2),req.radius)
    if c := cache_get(k,1800): return c
    if not ANTHROPIC_KEY: return {"places":[]}
    prompt = f"""Location: {req.city}{', '+req.state if req.state else ''} ({req.lat:.3f},{req.lng:.3f})
Radius: {req.radius} min drive. Generate 5 day trip towns.
Return ONLY JSON: [{{"id":int,"name":"Town, ST","emoji":"str","type":"trip","why":"2-3 sentences",
"tip":"insider tip","tags":["T1","T2","T3"],"buzz":float,"dist":"X hr Y min","driveMin":int}}]"""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","Content-Type":"application/json"},
                json={"model":"claude-sonnet-4-20250514","max_tokens":2000,"messages":[{"role":"user","content":prompt}]})
            raw = r.json().get("content",[{}])[0].get("text","[]")
            places = json.loads(raw.replace("```json","").replace("```","").strip())
            places = [p for p in places if isinstance(p,dict) and p.get("driveMin",999)<=req.radius]
            result={"places":places}; cache_set(k,result); return result
    except Exception as e: print(f"Trips: {e}"); return {"places":[]}

# ── TRACES ────────────────────────────────────────────────────

@app.get("/api/traces")
async def get_traces(lat: float=None, lng: float=None, place_name: str=None,
                     limit: int=20, offset: int=0):
    if not SUPABASE_URL: return {"traces":[],"total":0}
    try:
        params={"select":"*,profiles(name,avatar_url)","order":"created_at.desc",
                "limit":limit,"offset":offset}
        if place_name: params["place_name"]=f"ilike.*{place_name}*"
        data = await sb_get("traces",params)
        return {"traces":data or [],"total":len(data or [])}
    except Exception as e: print(f"Get traces: {e}"); return {"traces":[],"total":0}

async def _get_jwt_user(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401,"Authentication required")
    jwt = authorization.replace("Bearer ","")
    async with httpx.AsyncClient(timeout=6) as c:
        r = await c.get(f"{SUPABASE_URL}/auth/v1/user",
                       headers={"apikey":SUPABASE_KEY,"Authorization":f"Bearer {jwt}"})
        if r.status_code != 200: raise HTTPException(401,"Invalid token")
        return r.json()["id"], jwt

@app.post("/api/traces")
async def create_trace(trace: TraceCreate, authorization: str=Header(None)):
    if not SUPABASE_URL: raise HTTPException(503,"Database not configured")
    user_id, jwt = await _get_jwt_user(authorization)
    trace_id = str(uuid.uuid4())
    data = await sb_post("traces",{"id":trace_id,"user_id":user_id,
        "place_name":trace.place_name,"place_type":trace.place_type,
        "lat":trace.lat,"lng":trace.lng,"sentence":trace.sentence,
        "photo_url":"","day_story_id":trace.day_story_id},jwt=jwt)
    if not data: raise HTTPException(500,"Failed to create trace")
    return {"trace_id":trace_id}

@app.post("/api/traces/{trace_id}/photo")
async def upload_trace_photo(trace_id: str, file: UploadFile=File(...),
                             authorization: str=Header(None)):
    if not SUPABASE_URL: raise HTTPException(503,"Database not configured")
    if file.content_type not in ("image/jpeg","image/png","image/webp","image/heic"):
        raise HTTPException(400,"JPEG, PNG or WebP only")
    contents = await file.read()
    if len(contents) > 5*1024*1024: raise HTTPException(400,"Max 5MB")
    user_id, jwt = await _get_jwt_user(authorization)
    ext = file.content_type.split("/")[-1].replace("jpeg","jpg")
    photo_url = await sb_upload("traces",f"{user_id}/{trace_id}.{ext}",
                                contents,file.content_type,jwt)
    if not photo_url: raise HTTPException(500,"Upload failed")
    async with httpx.AsyncClient(timeout=8) as c:
        await c.patch(f"{SUPABASE_URL}/rest/v1/traces?id=eq.{trace_id}",
                      headers={**sb_headers(jwt),"Prefer":"return=minimal"},
                      json={"photo_url":photo_url})
    return {"photo_url":photo_url}

@app.delete("/api/traces/{trace_id}")
async def delete_trace(trace_id: str, authorization: str=Header(None)):
    _, jwt = await _get_jwt_user(authorization)
    async with httpx.AsyncClient(timeout=8) as c:
        r = await c.delete(f"{SUPABASE_URL}/rest/v1/traces?id=eq.{trace_id}",
                           headers=sb_headers(jwt))
    return {"ok":r.status_code < 300}

@app.get("/api/stories")
async def get_stories(city: str=None, limit: int=10):
    if not SUPABASE_URL: return {"stories":[]}
    params={"select":"*,profiles(name,avatar_url)","order":"created_at.desc","limit":limit}
    if city: params["city"]=f"ilike.*{city}*"
    return {"stories":await sb_get("day_stories",params) or []}

@app.post("/api/stories")
async def create_story(story: DayStoryCreate, authorization: str=Header(None)):
    if not SUPABASE_URL: raise HTTPException(503,"Database not configured")
    user_id, jwt = await _get_jwt_user(authorization)
    title = story.title
    if not title and ANTHROPIC_KEY:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post("https://api.anthropic.com/v1/messages",
                    headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","Content-Type":"application/json"},
                    json={"model":"claude-sonnet-4-20250514","max_tokens":60,
                          "messages":[{"role":"user","content":f"4-6 word poetic title for a day in {story.city} on {story.travel_date}. Just the title."}]})
                title = r.json().get("content",[{}])[0].get("text","").strip().strip('"')
        except: title = f"A day in {story.city}"
    data = await sb_post("day_stories",{"user_id":user_id,"city":story.city,
        "travel_date":story.travel_date,"title":title,"trace_ids":story.trace_ids},jwt=jwt)
    if not data: raise HTTPException(500,"Failed")
    return {"story_id":(data[0] if isinstance(data,list) else data).get("id"),"title":title}

@app.get("/api/stories/{story_id}")
async def get_story(story_id: str):
    if not SUPABASE_URL: raise HTTPException(404,"Not found")
    data = await sb_get(f"day_stories",{"id":f"eq.{story_id}",
                                        "select":"*,profiles(name,avatar_url)"})
    if not data: raise HTTPException(404,"Story not found")
    story = data[0] if isinstance(data,list) else data
    traces = await sb_get("traces_with_user",
                          {"id":f"in.({','.join(story.get('trace_ids',[])) or 'null'})","select":"*"}) or []
    return {**story,"traces":traces}

@app.post("/api/stories/{story_id}/copy")
async def copy_story(story_id: str):
    if not SUPABASE_URL: return {"ok":True}
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            await c.post(f"{SUPABASE_URL}/rest/v1/rpc/increment_copy_count",
                headers=sb_headers(),json={"story_id":story_id})
        return {"ok":True}
    except: return {"ok":False}

@app.get("/")
def root():
    for p in ["frontend/index.html","index.html"]:
        if os.path.exists(p): return FileResponse(p)
    return {"message":"TootsieBootsie API v3"}

@app.get("/{path:path}")
def catch_all(path: str):
    for base in ["frontend","."]:
        if os.path.exists(f"{base}/{path}"): return FileResponse(f"{base}/{path}")
    for p in ["frontend/index.html","index.html"]:
        if os.path.exists(p): return FileResponse(p)
    raise HTTPException(404,"Not found")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("index:app",host="0.0.0.0",port=8000,reload=True)
