import json

d = json.load(open(r'D:\project\AIProjects\ComfyUI_windows_portable\ComfyUI\minimax_h3_textonly_4060ti.json', encoding='utf-8'))
for n in d['nodes']:
    w = n.get('widgets_values', [])
    kv = []
    if isinstance(w, list):
        for i, x in enumerate(w):
            if isinstance(x, (str, int, float, bool)):
                kv.append(f'{i}:{str(x)[:70]}')
    elif isinstance(w, dict):
        for k, v in w.items():
            if isinstance(v, (str, int, float, bool)):
                kv.append(f'{k}={str(v)[:70]}')
    print(f"{n['id']:>5} {n['type']:<38} | {' ; '.join(kv)[:150]}")

print()
print('=== links ===')
for l in d['links']:
    print(f'{l[1]}.{l[2]} -> {l[3]}.{l[4]}')
