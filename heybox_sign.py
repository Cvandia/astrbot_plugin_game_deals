"""Public web URL checksum, ported from huandu/heybox-url (MIT).
Copyright (c) 2023 Huan Du. See licenses/heybox-url-MIT.txt.
"""
import hashlib
import time
import secrets
import re

D='JKMNPQRTX1234OABCDFG56789H'
def sign(path,params):
 ts=int(time.time());nonce=secrets.token_hex(16).upper();md5=lambda s:hashlib.md5(s.encode()).hexdigest()
 nh=md5(re.sub(r'\D','',nonce+D));rnd=re.sub(r'\D','',md5(str(ts+1)+'/'+path.strip('/')+'/'+nh))[:9].ljust(9,'0');c=int(rnd);key=''
 for i in range(5):key+=D[c%len(D)];c//=len(D)
 def b(v):return ((v<<1)^0x1b)&255 if v&128 else v<<1
 def c3(v):return b(v)^v
 def c2(v):return c3(b(v))
 def c1(v):return c2(c3(b(v)))
 def c0(v):return c1(v)^c2(v)^c3(v)
 a,b_,c,d=map(ord,key[-4:]);check=( (c0(a)^c1(b_)^c2(c)^c3(d))+(c3(a)^c0(b_)^c1(c)^c2(d))+(c2(a)^c3(b_)^c0(c)^c1(d))+(c1(a)^c2(b_)^c3(c)^c0(d)))%100
 return {**params,'hkey':key+f'{check:02d}','_time':ts,'nonce':nonce}
