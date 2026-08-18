#!/usr/bin/env python3
import os, re, sys, json, shutil, hashlib, zipfile, urllib.request, subprocess
from pathlib import Path

BASE_URL = os.environ.get('ARENA_BASE_URL','').strip()
ROOT = Path.cwd()
WORK = ROOT / 'arena2i_build'
SRC = WORK / 'src'
OUT = ROOT / 'build_output'
RELEASE='1.2.0'
SCHEMA='8'
RUNTIME='21'
RUNTIME_CONTRACT='arena_server_offline_auto_execution:v1'
OFFLINE_CONTRACT='arena_offline_auto_execution:v1'
OFFLINE_VERSION='1'
PKG=f'zenofusion-arena-batch-2I-server-side-offline-auto-execution-{RELEASE}.zip'


def die(msg):
    print('ERROR:', msg, file=sys.stderr)
    sys.exit(1)

def sha256_bytes(b): return hashlib.sha256(b).hexdigest()
def sha256_file(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for c in iter(lambda:f.read(1024*1024), b''): h.update(c)
    return h.hexdigest()

def read(p): return Path(p).read_text(encoding='utf-8')
def write(p,s):
    p=Path(p); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(s,encoding='utf-8')

def php_service():
    return r'''<?php
if (!defined('DATALIFEENGINE')) { die('Hacking attempt!'); }

if (!defined('ZFAR_OFFLINE_AUTO_CONTRACT_VERSION')) define('ZFAR_OFFLINE_AUTO_CONTRACT_VERSION', 1);
if (!defined('ZFAR_OFFLINE_AUTO_CONTRACT')) define('ZFAR_OFFLINE_AUTO_CONTRACT', 'arena_offline_auto_execution:v1');

function zfar_offline_auto_contract_info() {
    return array(
        'contract' => ZFAR_OFFLINE_AUTO_CONTRACT,
        'contract_version' => (int) ZFAR_OFFLINE_AUTO_CONTRACT_VERSION,
        'server_authoritative' => true,
        'browser_dependency' => false,
        'client_timer_dependency' => false,
        'client_stat_math_dependency' => false,
        'mixed_play_modes' => true,
        'formats' => array('DUEL','FOUR_WAY'),
        'action_sources' => array('auto','away_auto','timeout_auto'),
        'duplicate_worker_guard' => 'idempotency+state_revision',
        'downstream_action_engine' => 'deferred_phase_iii',
        'settlement' => 'deferred',
        'rest' => 'deferred',
        'operational' => false,
    );
}

function zfar_offline_auto_service_status() {
    $c = zfar_offline_auto_contract_info();
    return array(
        'ready' => true,
        'worker_boundary_ready' => true,
        'operational' => false,
        'contract' => $c['contract'],
        'contract_version' => $c['contract_version'],
        'reason' => 'downstream_action_engine_deferred',
    );
}

function zfar_offline_auto_action_source(array $player) {
    $mode = strtoupper(trim((string)($player['play_mode'] ?? 'MANUAL')));
    if ($mode !== 'AUTO') return 'manual';
    $reason = strtolower(trim((string)($player['auto_reason'] ?? '')));
    $presence = strtoupper(trim((string)($player['presence_state'] ?? 'UNKNOWN')));
    if ($reason === 'timeout') return 'timeout_auto';
    if ($reason === 'away_auto' || !empty($player['auto_away_eligible']) || $presence === 'AWAY') return 'away_auto';
    return 'auto';
}

function zfar_offline_auto_turn_key(array $ctx) {
    $parts = array(
        'v1',
        (string)($ctx['match_ref'] ?? $ctx['match_id'] ?? ''),
        (string)($ctx['round_no'] ?? $ctx['current_round'] ?? 0),
        (string)($ctx['step_index'] ?? 0),
        (string)($ctx['match_player_id'] ?? 0),
        (string)($ctx['state_revision'] ?? 0),
    );
    return 'offline_auto:' . hash('sha256', implode('|', $parts));
}

function zfar_offline_auto_reject_client_authority(array $ctx) {
    $forbidden = array('client_stats','stats','card_stats','comparison','comparison_result','client_math','card_math','client_timer','deadline_remaining','winner','capture_result');
    foreach ($forbidden as $k) if (array_key_exists($k, $ctx)) return array('ok'=>false,'code'=>'client_authority_rejected','field'=>$k);
    return array('ok'=>true);
}

function zfar_offline_auto_claim_turn($db, array $ctx) {
    $key = zfar_offline_auto_turn_key($ctx);
    if (!function_exists('zfar_idempotency_claim')) {
        return array('ok'=>false,'code'=>'idempotency_primitive_unavailable','idempotency_key'=>$key);
    }
    $payload = array(
        'match_id'=>(int)($ctx['match_id'] ?? 0),
        'round_no'=>(int)($ctx['round_no'] ?? $ctx['current_round'] ?? 0),
        'step_index'=>(int)($ctx['step_index'] ?? 0),
        'match_player_id'=>(int)($ctx['match_player_id'] ?? 0),
        'state_revision'=>(int)($ctx['state_revision'] ?? 0),
    );
    try {
        $rf = new ReflectionFunction('zfar_idempotency_claim');
        $n = $rf->getNumberOfParameters();
        if ($n >= 5) $r = zfar_idempotency_claim($db, 'arena_offline_auto_turn', $key, 'offline_auto_execute', $payload);
        elseif ($n === 4) $r = zfar_idempotency_claim($db, 'arena_offline_auto_turn', $key, $payload);
        elseif ($n === 3) $r = zfar_idempotency_claim($db, 'arena_offline_auto_turn', $key);
        else return array('ok'=>false,'code'=>'idempotency_signature_unsupported','idempotency_key'=>$key);
        if (is_array($r)) $r['idempotency_key'] = $key;
        return is_array($r) ? $r : array('ok'=>(bool)$r,'idempotency_key'=>$key);
    } catch (Throwable $e) {
        return array('ok'=>false,'code'=>'idempotency_claim_failed','message'=>$e->getMessage(),'idempotency_key'=>$key);
    }
}

function zfar_offline_auto_finish_claim($db, array $claim, $success, array $result=array()) {
    $key = (string)($claim['idempotency_key'] ?? '');
    if ($key === '') return;
    $fn = $success ? 'zfar_idempotency_complete' : 'zfar_idempotency_fail';
    if (!function_exists($fn)) return;
    try {
        $rf = new ReflectionFunction($fn); $n=$rf->getNumberOfParameters();
        if ($n >= 4) $fn($db, 'arena_offline_auto_turn', $key, $result);
        elseif ($n === 3) $fn($db, 'arena_offline_auto_turn', $key);
        elseif ($n === 2) $fn($db, $key);
    } catch (Throwable $e) { /* fail closed: canonical action layer remains authority */ }
}

function zfar_offline_auto_dispatcher() {
    foreach (array('zfar_auto_execute_server_turn','zfar_server_auto_execute_turn') as $fn) if (function_exists($fn)) return $fn;
    return null;
}

function zfar_offline_auto_execute_one($db, array $ctx, $dispatcher=null) {
    $guard = zfar_offline_auto_reject_client_authority($ctx);
    if (empty($guard['ok'])) return $guard;
    $source = zfar_offline_auto_action_source($ctx);
    if ($source === 'manual') return array('ok'=>false,'code'=>'manual_seat_not_auto','deferred'=>true);
    if ($dispatcher === null) $dispatcher = zfar_offline_auto_dispatcher();
    if (!$dispatcher || !is_callable($dispatcher)) {
        return array('ok'=>false,'code'=>'downstream_action_engine_deferred','deferred'=>true,'action_source'=>$source);
    }
    $claim = zfar_offline_auto_claim_turn($db, $ctx);
    if (empty($claim['ok'])) return array_merge($claim,array('action_source'=>$source));
    $serverCtx = array(
        'match_id'=>(int)($ctx['match_id'] ?? 0),
        'match_ref'=>(string)($ctx['match_ref'] ?? ''),
        'match_player_id'=>(int)($ctx['match_player_id'] ?? 0),
        'round_no'=>(int)($ctx['round_no'] ?? $ctx['current_round'] ?? 0),
        'step_index'=>(int)($ctx['step_index'] ?? 0),
        'state_revision'=>(int)($ctx['state_revision'] ?? 0),
        'match_format'=>strtoupper((string)($ctx['match_format'] ?? '')),
        'action_source'=>$source,
        'server_authoritative'=>true,
        'idempotency_key'=>(string)($claim['idempotency_key'] ?? ''),
    );
    try {
        $result = call_user_func($dispatcher, $db, $serverCtx);
        if (!is_array($result)) $result=array('ok'=>(bool)$result);
        zfar_offline_auto_finish_claim($db,$claim,!empty($result['ok']),$result);
        $result['action_source']=$source;
        $result['idempotency_key']=$serverCtx['idempotency_key'];
        return $result;
    } catch (Throwable $e) {
        $r=array('ok'=>false,'code'=>'offline_auto_dispatch_failed','message'=>$e->getMessage(),'action_source'=>$source);
        zfar_offline_auto_finish_claim($db,$claim,false,$r);
        return $r;
    }
}

function zfar_offline_auto_worker_tick($db, $limit=10, $dispatcher=null) {
    $limit=max(1,min(50,(int)$limit));
    if (!$dispatcher) $dispatcher=zfar_offline_auto_dispatcher();
    if (!$dispatcher || !is_callable($dispatcher)) return array('ok'=>false,'code'=>'downstream_action_engine_deferred','deferred'=>true,'scanned'=>0,'executed'=>0,'limit'=>$limit);
    if (!function_exists('zfar_schema_tables')) return array('ok'=>false,'code'=>'arena_schema_unavailable','scanned'=>0,'executed'=>0);
    $tables=zfar_schema_tables();
    if (empty($tables['matches']) || empty($tables['match_players'])) return array('ok'=>false,'code'=>'arena_match_tables_unavailable','scanned'=>0,'executed'=>0);
    $sql="SELECT mp.*,m.match_ref,m.match_format,m.current_round,m.state_revision FROM `{$tables['match_players']}` mp INNER JOIN `{$tables['matches']}` m ON m.match_id=mp.match_id WHERE m.status='active' AND mp.play_mode='AUTO' ORDER BY m.match_id ASC,mp.match_player_id ASC LIMIT {$limit}";
    $rows=array();
    try {
        $db->query($sql);
        while ($r=$db->get_row()) $rows[]=$r;
    } catch (Throwable $e) { return array('ok'=>false,'code'=>'offline_auto_worker_scan_failed','message'=>$e->getMessage(),'scanned'=>0,'executed'=>0); }
    $results=array(); $executed=0;
    foreach ($rows as $row) {
        $r=zfar_offline_auto_execute_one($db,$row,$dispatcher); $results[]=$r;
        if (!empty($r['ok'])) $executed++;
    }
    return array('ok'=>true,'scanned'=>count($rows),'executed'=>$executed,'limit'=>$limit,'results'=>$results);
}
'''

def pure_test(name, body):
    return "<?php\ndefine('DATALIFEENGINE', true);\nrequire_once __DIR__ . '/../engine/inc/zf_arena/offline_auto_service.php';\n" + body + "\necho 'PASS — " + name + "\\n';\n"

def patch_define(text,name,value,quote=True):
    val=("'"+value+"'") if quote else str(value)
    pats=[
      rf"(define\(\s*['\"]{re.escape(name)}['\"]\s*,\s*)(['\"][^'\"]*['\"]|\d+)(\s*\)\s*;)",
      rf"(const\s+{re.escape(name)}\s*=\s*)(['\"][^'\"]*['\"]|\d+)(\s*;)"
    ]
    for p in pats:
        new,n=re.subn(p,rf"\g<1>{val}\g<3>",text,count=1)
        if n: return new,True
    return text,False

def embed_existing(xml,path,content):
    pattern=re.compile(r'(<file\b[^>]*(?:name|path)=["\']'+re.escape(path)+r'["\'][^>]*>)(.*?)(</file>)',re.S|re.I)
    m=pattern.search(xml)
    if not m: return xml,False
    body=m.group(2)
    newbody,n=re.subn(r'<!\[CDATA\[.*?\]\]>', '<![CDATA['+content+']]>', body, count=1, flags=re.S)
    if not n: return xml,False
    return xml[:m.start()]+m.group(1)+newbody+m.group(3)+xml[m.end():],True

def clone_file_node(xml,source_path,new_path,new_content):
    pat=re.compile(r'<file\b[^>]*(?:name|path)=["\']'+re.escape(source_path)+r'["\'][^>]*>.*?</file>',re.S|re.I)
    m=pat.search(xml)
    if not m: return xml,False
    node=m.group(0).replace(source_path,new_path,1)
    node,n=re.subn(r'<!\[CDATA\[.*?\]\]>', '<![CDATA['+new_content+']]>', node, count=1, flags=re.S)
    if not n: return xml,False
    return xml[:m.end()]+'\n'+node+xml[m.end():],True

if not BASE_URL: die('ARENA_BASE_URL missing')
shutil.rmtree(WORK, ignore_errors=True); shutil.rmtree(OUT, ignore_errors=True)
SRC.mkdir(parents=True); OUT.mkdir(parents=True)
base=WORK/'base.zip'
print('Downloading 2H base...')
urllib.request.urlretrieve(BASE_URL, base)
with zipfile.ZipFile(base) as z: z.extractall(SRC)

# locate package root if archive has one wrapper directory
if not (SRC/'engine').exists():
    dirs=[p for p in SRC.iterdir() if p.is_dir()]
    if len(dirs)==1 and (dirs[0]/'engine').exists(): SRC=dirs[0]

version=SRC/'engine/inc/zf_arena/version.php'
bootstrap=SRC/'engine/inc/zf_arena/bootstrap.php'
service=SRC/'engine/inc/zf_arena/service.php'
admin=SRC/'engine/inc/zf_arena/admin.php'
offline=SRC/'engine/inc/zf_arena/offline_auto_service.php'
xml=SRC/'zenofusion-arena.xml'
for p in [version,bootstrap,service,admin,xml]:
    if not p.exists(): die(f'missing {p}')

svc=php_service(); write(offline,svc)

v=read(version)
for name,val,q in [('ZFAR_VERSION',RELEASE,True),('ZFAR_RUNTIME_CONTRACT_VERSION',RUNTIME,False),('ZFAR_RUNTIME_CONTRACT',RUNTIME_CONTRACT,True),('ZFAR_CURRENT_BATCH','2I',True)]:
    v,ok=patch_define(v,name,val,q)
    if not ok:
        if name=='ZFAR_VERSION': v=v.replace("'1.1.9'", "'1.2.0'",1).replace('"1.1.9"','"1.2.0"',1)
        elif name=='ZFAR_RUNTIME_CONTRACT_VERSION': v=re.sub(r'(RUNTIME_CONTRACT_VERSION[^\n]*?)20',r'\g<1>21',v,count=1)
        elif name=='ZFAR_RUNTIME_CONTRACT': v=v.replace("'arena_away_auto_room_lifecycle:v1'","'arena_server_offline_auto_execution:v1'",1)
        elif name=='ZFAR_CURRENT_BATCH': v=re.sub(r'(CURRENT_BATCH[^\n]*?)["\']2H["\']',r"\g<1>'2I'",v,count=1)
append="\nif (!defined('ZFAR_OFFLINE_AUTO_CONTRACT_VERSION')) define('ZFAR_OFFLINE_AUTO_CONTRACT_VERSION', 1);\nif (!defined('ZFAR_OFFLINE_AUTO_CONTRACT')) define('ZFAR_OFFLINE_AUTO_CONTRACT', 'arena_offline_auto_execution:v1');\n"
if 'ZFAR_OFFLINE_AUTO_CONTRACT' not in v:
    v=v.replace('?>',append+'?>') if '?>' in v else v+append
write(version,v)

b=read(bootstrap)
if 'offline_auto_service.php' not in b:
    lines=b.splitlines(True); idx=None
    for i,l in enumerate(lines):
        if 'away_auto_service.php' in l: idx=i+1; template=l; break
    if idx is None: die('bootstrap away_auto_service require not found')
    lines.insert(idx, template.replace('away_auto_service.php','offline_auto_service.php'))
    b=''.join(lines)
write(bootstrap,b)

s=read(service)
if 'zfar_offline_auto_service_status' not in s:
    extra="\n/* Batch 2I server-side Offline Auto readiness boundary. */\nfunction zfar_offline_auto_runtime_status() {\n    return function_exists('zfar_offline_auto_service_status') ? zfar_offline_auto_service_status() : array('ready'=>false,'operational'=>false,'reason'=>'offline_auto_service_missing');\n}\n"
    s=s.replace('?>',extra+'?>') if '?>' in s else s+extra
write(service,s)

# Admin remains structurally stable; expose diagnostic helper without altering existing layout.
a=read(admin)
if 'zfar_admin_offline_auto_diagnostic' not in a:
    extra="\n/* Batch 2I diagnostic helper: consumed by later functional Admin monitor. */\nfunction zfar_admin_offline_auto_diagnostic() {\n    $s=function_exists('zfar_offline_auto_service_status') ? zfar_offline_auto_service_status() : array('ready'=>false,'operational'=>false);\n    return array('label'=>'Offline Auto Service','ready'=>!empty($s['ready']),'operational'=>!empty($s['operational']),'downstream'=>'DEFERRED');\n}\n"
    a=a.replace('?>',extra+'?>') if '?>' in a else a+extra
write(admin,a)

# README + tests
readme=f'''# ZenoFusion Arena — Batch 2I\n\nRelease: {RELEASE}\nSchema: {SCHEMA} (unchanged)\nRuntime contract: {RUNTIME} / {RUNTIME_CONTRACT}\nOffline Auto contract: {OFFLINE_CONTRACT}\n\nBatch 2I adds the server-side Offline Auto execution boundary. It supports LIVE·AUTO, AWAY·AUTO and timeout AUTO source classification in both DUEL and FOUR_WAY, rejects client-submitted stat/comparison/timer authority, builds deterministic turn idempotency keys, and requires the existing Arena idempotency primitives before dispatch. A bounded worker scans AUTO match players only.\n\nThis batch deliberately does not implement the Phase III card-action/round engine. If the canonical downstream server Auto action dispatcher is not available, execution returns `downstream_action_engine_deferred` and performs no battle mutation. Settlement, capture ownership transfer, Battle Statistics and Rest remain deferred to their roadmap batches.\n\nNo direct Cards SQL is added. Schema remains 8.\n'''
write(SRC/'BATCH-2I-README.md',readme)

write(SRC/'tests/zfar_batch_2i_offline_auto_regression.php',pure_test('Batch 2I Offline Auto regression',r'''$c=zfar_offline_auto_contract_info();
if ($c['contract']!=='arena_offline_auto_execution:v1') exit(1);
if (!$c['server_authoritative'] || $c['browser_dependency'] || $c['client_timer_dependency'] || $c['client_stat_math_dependency']) exit(2);
if ($c['operational']!==false || $c['downstream_action_engine']!=='deferred_phase_iii') exit(3);
'''))
write(SRC/'tests/zfar_batch_2i_action_source_smoke.php',pure_test('Batch 2I action-source smoke',r'''if(zfar_offline_auto_action_source(['play_mode'=>'AUTO','presence_state'=>'LIVE','auto_reason'=>'user'])!=='auto') exit(1);
if(zfar_offline_auto_action_source(['play_mode'=>'AUTO','presence_state'=>'AWAY','auto_reason'=>'away_auto','auto_away_eligible'=>1])!=='away_auto') exit(2);
if(zfar_offline_auto_action_source(['play_mode'=>'AUTO','presence_state'=>'LIVE','auto_reason'=>'timeout'])!=='timeout_auto') exit(3);
if(zfar_offline_auto_action_source(['play_mode'=>'MANUAL'])!=='manual') exit(4);
'''))
write(SRC/'tests/zfar_batch_2i_duplicate_worker_smoke.php',pure_test('Batch 2I duplicate-worker smoke',r'''$x=['match_ref'=>'M01','round_no'=>2,'step_index'=>1,'match_player_id'=>7,'state_revision'=>9];
$a=zfar_offline_auto_turn_key($x); $b=zfar_offline_auto_turn_key($x);
if($a!==$b || strpos($a,'offline_auto:')!==0) exit(1);
$x['state_revision']=10; if(zfar_offline_auto_turn_key($x)===$a) exit(2);
$r=zfar_offline_auto_execute_one(null,['play_mode'=>'AUTO','presence_state'=>'AWAY','match_id'=>1],null);
if(($r['code']??'')!=='downstream_action_engine_deferred' || empty($r['deferred'])) exit(3);
'''))
write(SRC/'tests/zfar_batch_2i_phase_continuity_smoke.php',"<?php\ndefine('DATALIFEENGINE', true);\n$root=dirname(__DIR__);\n$must=['primitives.php','cards_adapter.php','deck_service.php','room_service.php','match_service.php','comparator.php','presence_service.php','away_auto_service.php','offline_auto_service.php'];\nforeach($must as $f) if(!is_file($root.'/engine/inc/zf_arena/'.$f)) exit(1);\n$v=file_get_contents($root.'/engine/inc/zf_arena/version.php');\nif(strpos($v,'1.2.0')===false || strpos($v,'arena_server_offline_auto_execution:v1')===false) exit(2);\n$o=file_get_contents($root.'/engine/inc/zf_arena/offline_auto_service.php');\nforeach(['auto','away_auto','timeout_auto','downstream_action_engine_deferred','zfar_offline_auto_worker_tick'] as $s) if(strpos($o,$s)===false) exit(3);\nif(preg_match('/zf_cards|zf_card_instances|UPDATE\\s+.*cards/i',$o)) exit(4);\necho 'PASS — Batch 2I Phase II continuity smoke\\n';\n")

# Update installer XML: first top-level values, then embedded runtime files, then clone new file node.
x=read(xml)
x=x.replace('<version>1.1.9</version>','<version>1.2.0</version>',1)
# bounded current metadata replacements
x=re.sub(r"(['\"]current_batch['\"]\s*,\s*)['\"]2H['\"]",r"\g<1>'2I'",x,count=1)
x=re.sub(r"(['\"]runtime_contract_version['\"]\s*,\s*)['\"]?20['\"]?",r"\g<1>'21'",x,count=1)
x=re.sub(r"(['\"]runtime_contract['\"]\s*,\s*)['\"]arena_away_auto_room_lifecycle:v1['\"]",r"\g<1>'arena_server_offline_auto_execution:v1'",x,count=1)
# common embedded files
for path in ['engine/inc/zf_arena/version.php','engine/inc/zf_arena/bootstrap.php','engine/inc/zf_arena/service.php','engine/inc/zf_arena/admin.php']:
    x,ok=embed_existing(x,path,read(SRC/path))
    if not ok: die('cannot update embedded '+path)
x,ok=clone_file_node(x,'engine/inc/zf_arena/away_auto_service.php','engine/inc/zf_arena/offline_auto_service.php',svc)
if not ok: die('cannot clone installer file node for offline_auto_service.php')
# ensure release markers in XML even if version tag syntax differs
if '1.2.0' not in x: die('XML release marker missing')
if 'offline_auto_service.php' not in x: die('XML offline service missing')
write(xml,x)

# Patch package-level current markers if present in meta/service/admin embedded content was already refreshed.
# Manifest includes all package files except manifest itself and output ZIP.
manifest=SRC/'PACKAGE-MANIFEST-2I.txt'
entries=[]
for p in sorted(SRC.rglob('*')):
    if p.is_file() and p.name!='PACKAGE-MANIFEST-2I.txt':
        rel=p.relative_to(SRC).as_posix(); entries.append(f"{sha256_file(p)}  {rel}")
write(manifest, f"ZenoFusion Arena Batch 2I\nRelease {RELEASE}\nSchema {SCHEMA}\nRuntime {RUNTIME} {RUNTIME_CONTRACT}\n\n"+'\n'.join(entries)+'\n')

# Validate syntax and contracts.
subprocess.run(['python3','-c',"import xml.etree.ElementTree as E; E.parse(r'%s')"%str(xml)],check=True)
for p in SRC.rglob('*.php'):
    r=subprocess.run(['php','-l',str(p)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    if r.returncode: die(r.stdout)
for t in ['zfar_batch_2i_offline_auto_regression.php','zfar_batch_2i_action_source_smoke.php','zfar_batch_2i_duplicate_worker_smoke.php','zfar_batch_2i_phase_continuity_smoke.php']:
    subprocess.run(['php',str(SRC/'tests'/t)],check=True)
js=list(SRC.rglob('*.js'))
for p in js: subprocess.run(['node','--check',str(p)],check=True)

# Verify no direct Cards SQL in new service.
low=svc.lower()
if re.search(r'\b(select|update|insert|delete)\b[^\n;]*\bzf_.*cards',low): die('direct Cards SQL detected')

# Create deterministic ZIP with package tree at root.
outzip=OUT/PKG
with zipfile.ZipFile(outzip,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
    for p in sorted(SRC.rglob('*')):
        if p.is_file():
            zi=zipfile.ZipInfo(p.relative_to(SRC).as_posix(),(2026,8,18,18,0,0)); zi.compress_type=zipfile.ZIP_DEFLATED; zi.external_attr=0o644<<16
            z.writestr(zi,p.read_bytes())
sha=sha256_file(outzip)
write(OUT/(PKG+'.sha256'),sha+'  '+PKG+'\n')
# standalone deliverables
for p in [xml, SRC/'BATCH-2I-README.md', manifest, offline]: shutil.copy2(p,OUT/p.name)
shutil.copy2(offline, OUT/'arena-server-offline-auto-service-2I.php')
for p in (SRC/'tests').glob('zfar_batch_2i_*.php'): shutil.copy2(p,OUT/p.name)
print('BUILD PASS',PKG,sha)
print('FILES',len([p for p in SRC.rglob('*') if p.is_file()]))
