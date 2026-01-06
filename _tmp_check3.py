import pandas as pd, unicodedata

def norm(s):
    s = str(s or '').strip().lower().replace('\n',' ')
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = ''.join(c for c in s if c.isalnum() or c.isspace())
    s = ' '.join(s.split())
    return s

def ascii_no_diacritics(s):
    x = unicodedata.normalize('NFD', s)
    x = ''.join(c for c in x if not unicodedata.combining(c))
    x = x.replace('đ','d').replace('Đ','D')
    return x

df = pd.read_csv('mon_hoc_tong_hop.csv', encoding='utf-8-sig')
name = 'Nguyễn Minh Hiến'
mask = df['Giang vien'].map(norm) == norm(name)
sub = df[mask]
mons = sorted({m.strip() for m in sub['Mon hoc'].astype(str).tolist() if m and m!='nan'})
print('Count:', len(mons))
for m in mons[:100]:
    print('-', ascii_no_diacritics(m))
