import ast, sys, json
from collections import Counter

results = {}
py_files = []
import subprocess
for f in subprocess.run(['find', '.', '-name', '*.py'], capture_output=True, text=True).stdout.strip().split('\n'):
    if f: py_files.append(f)

# 1. Bare except / except: pass
except_pass = []
except_broad = []
bare_except = []
for f in py_files:
    try:
        with open(f) as fh:
            tree = ast.parse(fh.read(), f)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    bare_except.append({'file': f, 'line': node.lineno})
                elif hasattr(node.type, 'id') and node.type.id == 'Exception':
                    if node.body and isinstance(node.body[0], ast.Pass):
                        except_pass.append({'file': f, 'line': node.lineno})
                    else:
                        except_broad.append({'file': f, 'line': node.lineno})
    except: pass

results['bare_except'] = bare_except
results['except_pass'] = except_pass
results['except_broad'] = except_broad

# 2. File sizes (God Object detection)
sizes = {}
for f in py_files:
    try:
        with open(f) as fh:
            sizes[f] = len(fh.readlines())
    except: pass
results['file_sizes'] = dict(sorted(sizes.items(), key=lambda x: -x[1])[:20])

# 3. Class sizes
class_sizes = []
for f in py_files:
    try:
        with open(f) as fh:
            tree = ast.parse(fh.read(), f)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                end = max([n.lineno for n in ast.walk(node)], default=node.lineno)
                class_sizes.append({'file': f, 'class': node.name, 'lines': end - node.lineno + 1})
    except: pass
class_sizes.sort(key=lambda x: -x['lines'])
results['class_sizes'] = class_sizes[:15]

# 4. Imports analysis
import_count = Counter()
for f in py_files:
    try:
        with open(f) as fh:
            tree = ast.parse(fh.read(), f)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    import_count[alias.name] += 1
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    import_count[node.module] += 1
    except: pass
results['top_imports'] = dict(import_count.most_common(20))

# 5. Test coverage
test_files = [f for f in py_files if 'test' in f.lower()]
results['test_file_count'] = len(test_files)
results['test_files'] = test_files[:30]

# 6. Bare except file distribution
from collections import Counter as C
dist = C(x['file'] for x in bare_except + except_pass)
results['except_distribution'] = dict(dist.most_common(10))

print(json.dumps(results, indent=2))
