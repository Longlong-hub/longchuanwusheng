"""Build and retain an independent copy of a hash-pinned public website release."""
from pathlib import Path,PurePosixPath
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import quote,urlsplit,unquote
import argparse,base64,hashlib,json,os,re,shutil,subprocess,urllib.request,time
ROOT=Path(__file__).resolve().parents[1]

def adapt(text,source,target,base):
    text=text.replace(source,target)
    def prefix(m):
        q,p=m.groups()
        return m.group(0) if p.startswith(('//','/#')) or p==base or p.startswith(base+'/') else q+base+p
    text=re.sub(r"([\"'])(/[^\"'\s<>]*)",prefix,text)
    return re.sub(r'url\((/[^)\s]+)\)',lambda m:m.group(0) if m.group(1).startswith((base+'/','//')) else 'url('+base+m.group(1)+')',text)

def verify(output,base,target):
    issues=[];pages=0;links=0;images=0
    class Inspector(HTMLParser):
        def handle_starttag(self,tag,attrs):
            nonlocal links,images
            for key,value in attrs:
                if key not in {'href','src'} or not value:continue
                if value.startswith('/') and not value.startswith('//'):
                    if not value.startswith(base+'/'):issues.append('Unprefixed '+value);continue
                    path=urlsplit(value).path[len(base):]
                elif value.startswith(target+'/'):path=urlsplit(value).path[len(base):]
                else:continue
                links+=1;images+=int(tag=='img')
                dest=output/unquote(path.lstrip('/'))
                if path.endswith('/') or not path:dest=dest/'index.html'
                if not dest.is_file():issues.append('Missing '+path)
    for f in output.rglob('*.html'):
        pages+=1;Inspector().feed(f.read_text(encoding='utf-8'))
    js=(output/'assets/app.js').read_text(encoding='utf-8')
    if "fetch('"+base+"/data/poems.json')" not in js:issues.append('Random poem data path')
    if "link.href='"+base+"/poetry/'+p.year+'/#'+p.anchor" not in js:issues.append('Random poem destination and fragment')
    if issues:raise RuntimeError(json.dumps(issues[:20],ensure_ascii=False))
    return {'html_pages':pages,'internal_targets':links,'image_references':images,'issues':[]}

def retain(output,repository):
    if repository!='Longlong-hub/longchuanwusheng':raise RuntimeError('Unexpected repository')
    token=os.environ.get('GH_TOKEN')
    if not token:raise RuntimeError('The workflow repository token is missing')
    auth=base64.b64encode(('x-access-token:'+token).encode()).decode()
    env=dict(os.environ,LCWS_GIT_AUTH='AUTHORIZATION: basic '+auth,GIT_TERMINAL_PROMPT='0',
        GIT_AUTHOR_NAME='github-actions[bot]',GIT_COMMITTER_NAME='github-actions[bot]',
        GIT_AUTHOR_EMAIL='41898282+github-actions[bot]@users.noreply.github.com',GIT_COMMITTER_EMAIL='41898282+github-actions[bot]@users.noreply.github.com')
    def git(*args):
        command=['git','-c','credential.helper=','--config-env=http.https://github.com/.extraHeader=LCWS_GIT_AUTH',*args]
        r=subprocess.run(command,cwd=output,env=env,text=True,capture_output=True)
        if r.returncode:raise RuntimeError('Git operation failed: '+r.stderr.replace(token,'[redacted]').replace(auth,'[redacted]')[-1000:])
        return r.stdout.strip()
    remote='https://github.com/'+repository+'.git'
    git('init','--initial-branch=gh-pages');git('add','--all','--','.')
    tree=git('write-tree');parents=[]
    if git('ls-remote',remote,'refs/heads/gh-pages'):
        git('fetch','--depth=1',remote,'refs/heads/gh-pages');parents=['-p',git('rev-parse','FETCH_HEAD')]
    commit=git('commit-tree',tree,*parents,'-m','Publish verified website snapshot')
    git('push',remote,commit+':refs/heads/gh-pages')
    if git('ls-remote',remote,'refs/heads/gh-pages').split()[0]!=commit:raise RuntimeError('Remote snapshot verification failed')
    # Keep Git metadata out of the Pages deployment artifact.
    metadata=output/'.git'
    if metadata.resolve().parent!=output.resolve():raise RuntimeError('Unsafe staging metadata path')
    shutil.rmtree(metadata)
    return commit

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--local-source',type=Path);parser.add_argument('--output',type=Path,default=ROOT/'_site');parser.add_argument('--retain',action='store_true');args=parser.parse_args()
    manifest=json.loads((ROOT/'source-manifest.json').read_text(encoding='utf-8'));output=args.output.resolve()
    if output.exists() and any(output.iterdir()):raise RuntimeError('Output must be new or empty')
    output.mkdir(parents=True,exist_ok=True)
    source=manifest['source_origin'];target=manifest['target_url'];base=manifest['base_path']
    if source!='https://longchuanwusheng.drtangjunlong.chatgpt.site' or target!='https://longlong-hub.github.io/longchuanwusheng':raise RuntimeError('Unexpected release origins')
    def download(entry):
        relative=PurePosixPath(entry['path'])
        if relative.is_absolute() or '..' in relative.parts or any(x.startswith('.') for x in relative.parts):raise RuntimeError('Unsafe release path')
        if args.local_source:data=(args.local_source/Path(*relative.parts)).read_bytes()
        elif entry.get('inline_file'):
            inline=PurePosixPath(entry['inline_file'])
            if inline.is_absolute() or '..' in inline.parts:raise RuntimeError('Unsafe inline source path')
            data=(ROOT/Path(*inline.parts)).read_bytes()
        else:
            for attempt in range(3):
                try:
                    req=urllib.request.Request(source+'/'+quote(relative.as_posix()),headers={'User-Agent':'longchuan-reviewed-site-mirror'})
                    with urllib.request.urlopen(req,timeout=90) as response:data=response.read(entry['size']+1)
                    break
                except Exception:
                    if attempt==2:raise
                    time.sleep(attempt+1)
        if len(data)!=entry['size'] or hashlib.sha256(data).hexdigest()!=entry['sha256']:raise RuntimeError('Release hash mismatch: '+entry['path']+' expected '+str(entry['size'])+' bytes; received '+str(len(data))+' bytes')
        dest=output/Path(*relative.parts);dest.parent.mkdir(parents=True,exist_ok=True)
        if dest.suffix in {'.html','.xml','.json','.css','.js'}:dest.write_text(adapt(data.decode('utf-8'),source,target,base),encoding='utf-8')
        else:dest.write_bytes(data)
    with ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(download,manifest['files']))
    (output/'.nojekyll').write_text('',encoding='utf-8')
    checks=verify(output,base,target)
    result={'source_commit':manifest['source_commit'],'target_url':target+'/','files':len(manifest['files']),'checks':checks}
    if args.retain:result['snapshot_commit']=retain(output,os.environ.get('GITHUB_REPOSITORY',''))
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a',encoding='utf-8') as f:f.write('## Verified website copy\n\n'+json.dumps(result,ensure_ascii=False,indent=2)+'\n')
if __name__=='__main__':main()

