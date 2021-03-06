#!/usr/bin/python3
#
#                   srcxray - source code X-ray
#
# Analyzes interconnections between functions and structures in source code.
#
# Uses cscope and git grep --show-function to
# reveal references between identifiers.
#
# 2018 Constantine Shulyupin, const@MakeLinux.com
#

from inspect import currentframe, getframeinfo, getouterframes, stack
import random
import os
import sys
import collections
from munch import *
import subprocess
import re
import networkx as nx
# from networkx.drawing.nx_agraph import read_dot # changes order of successors
# from networkx.drawing.nx_pydot import read_dot # no bad
from networkx.generators.ego import *
from networkx.algorithms.dag import *
from networkx.utils import open_file, make_str
from pprint import pprint
import difflib
import glob
from pathlib import *
import pygraphviz  # python3-pygraphviz
import unittest
import types
from xml.dom.minidom import parse
import xml.dom.minidom

default_root = 'starts'
black_list = ('aligned unlikely typeof u32 '
              'PVOP_CALLEE0 PVOP_VCALLEE0 PVOP_VCALLEE1 if trace_hardirqs_off '
              'i NULL likely unlikely true false test_bit NAPI_GRO_CB clear_bit '
              'atomic_read preempt_disable preempt_enable container_of ENOSYS '
              'READ_ONCE u64 u8 _RET_IP_ ret current '
              'AT_FDCWD fdput EBADF file_inode '
              'ssize_t path_put __user '
              'list_empty memcpy size_t loff_t pos d_inode dput copy_to_user EIO bool out IS_ERR '
              'EPERM rcu_read_lock rcu_read_unlock spin_lock spin_unlock list_for_each_entry kfree '
              'GFP_KERNEL ENOMEM EFAULT ENOENT EAGAIN PTR_ERR PAGE_SHIFT PAGE_SIZE '
              'pgoff_t pte_t pmd_t HPAGE_PMD_NR PageLocked entry swp_entry_t next unlock_page spinlock_t end XXstart '
              ' VM_BUG_ON VM_BUG_ON_PAGE BDI_SHOW max '
              'ssize_t path_put __user '
              'list_del compound_head list_add cond_resched put_page nr_pages min spin_lock_irqsave IS_ENABLED '
              'EBUSY UL NODE_DATA pr_err memset list size ptl PAGE_MASK pr_info offset addr get_page sprintf '
              'INIT_LIST_HEAD NUMA_NO_NODE spin_unlock_irqrestore mutex_unlock mutex_lock '
              'page_to_nid page_to_pfn pfn page_zone pfn_to_page '
              'BUG BUG_ON flags WARN_ON_ONCE ENODEV cpu_to_le16 cpumask_bits '
              'ERR_PTR ENOTSUPP EOPNOTSUPP EOPNOTSUPP WARN_ON EINVAL i name '
              'sigset_t fdget put_user get_user copy_from_user LOOKUP_FOLLOW LOOKUP_EMPTY EINTR '
              'O_CLOEXEC err getname access_ok task_pid_vnr cred '
              'percpu_ref_put get_timespec64 sigdelsetmask ns_capable kzalloc capable f_mode O_LARGEFILE pos_from_hilo '
              'pr_debug error current_cred ESRCH f_path find_task_by_vpid '
              'retry LOOKUP_REVAL retry_estale user_path_at lookup_flags old '
              'current_user_ns spin_lock_irq spin_unlock_irq prepare_creds '
              'tasklist_lock commit_creds read_lock read_unlock SIGKILL SIGSTOP abort_creds fd_install '
              'real_mount FMODE_WRITE tv_nsec putname '
              ).split()


level_limit = 20
limit = 1000
n = 0
cflow_structs = False

scaled = False


def print_limited(a, out=None):
    out = out if out else sys.stdout
    out.write(str(a) + '\n')
    global n
    n += 1
    if n > limit + 1:
        out.write('...')
        sys.exit(1)
        # raise(Exception('Reached limit'))


def log(*args, **kwargs):
    s = str(*args).rstrip()
    frameinfo = getframeinfo(currentframe().f_back)
    print("%s:%d %s" % (frameinfo.filename, frameinfo.lineno, stack()[1][3]),
          s, file=sys.stderr, **kwargs)
    return s


def popen(p):
    return subprocess.check_output(p, shell=True).decode('utf-8').splitlines()


def extract_referer(line):
    line = re.sub(r'__ro_after_init', '', line)
    line = re.sub(r'FNAME\((\w+)\)', r'\1', line)
    line = re.sub(r'.*TRACE_EVENT.*', '', line)
    m = re.match(r'^[^\s]+=[^,]*\(\*(\b\w+)\)\s*[\(\[=][^;]*$', line)
    if not m:
        m = re.match(r'^[^\s]+=[^,]*(\b\w+)\s*[\(\[=][^;]*$', line)
    if m:
        return m.group(1)


def extract_referer_test():
    for a in {
            "fs=good2()",
            "f=static int fastop(struct x86_emulate_ctxt *ctxt, "
            + "void (*fop)(struct fastop *))",
            "f=int good(a, bad (*func)(arg))",
            "f=EXPORT_SYMBOL_GPL(bad);",
            "f=bad (*good)()",
            "f=int FNAME(good)(a)",
            "f=TRACE_EVENT(a)",
            "f: a=in bad()"}:
        print(a, '->', extract_referer(a))


def func_referers_git_grep(name):
    res = list()
    r = None
    for line in popen(r'git grep --threads 1 --no-index --word-regexp --show-function '
                      r'"^\s.*\b%s" '
                      r'**.\[hc\] **.cpp **.cc **.hh || true' % (name)):
        # Filter out names in comment afer function,
        # when comment start from ' *'
        # To see the problem try "git grep -p and"
        for p in {
                r'.*:\s+\* .*%s',
                r'.*/\*.*%s',
                r'.*//.*%s',
                r'.*".*\b%s\b.*"'}:
            if re.match(p % (name), line):
                r = None
                break
        if r and r != name and r not in black_list:
            res.append(r)
            r = None
        r = extract_referer(line)
    return res


cscope_warned = False


def func_referers_cscope(name):
    global cscope_warned
    if not os.path.isfile('cscope.out'):
        if not cscope_warned:
            print("Recommended: cscope -Rcbk", file=sys.stderr)
            cscope_warned = True
        return []
    res = list(dict.fromkeys([l.split()[1] for l in popen(r'cscope -d -L3 "%s"' %
                                                          (name)) if l not in black_list]))
    if not res:
        res = func_referers_git_grep(name)
    return res


def func_referers_all(name):
    return list(dict.fromkeys(func_referers_git_grep(name) + func_referers_cscope(name)))


def referers_tree(name, referer=None, printed=None, level=0):
    if not referer:
        if os.path.isfile('cscope.out'):
            referer = func_referers_cscope
        else:
            print("Using git grep only, recommended to run: cscope -Rcbk",
                  file=sys.stderr)
            referer = func_referers_git_grep
    if isinstance(referer, str):
        referer = eval(referer)
    if not printed:
        printed = set()
    if name in printed:
        print_limited(level*'\t' + name + ' ^')
        return
    else:
        print_limited(level*'\t' + name)
    printed.add(name)
    if level > level_limit - 2:
        print_limited((level + 1)*'\t' + '...')
        return ''
    for a in referer(name):
        referers_tree(a, referer, printed, level + 1)
    return ''


def referers_dep(name, referer=None, printed=None, level=0):
    if not referer:
        if os.path.isfile('cscope.out'):
            referer = func_referers_cscope
        else:
            print("Using git grep only, recommended to run: cscope -Rcbk",
                  file=sys.stderr)
            referer = func_referers_git_grep
    if isinstance(referer, str):
        referer = eval(referer)
    if not printed:
        printed = set()
    if name in printed:
        return
    if level > level_limit - 2:
        return ''
    referers = referer(name)
    if referers:
        printed.add(name)
        print("%s:" % (name), ' '.join(referers))
        for a in referers:
            referers_dep(a, referer, printed, level + 1)
    else:
        pass
        # TODO: print terminal
        # print('...')
    return ''


def call_tree(node, printed=None, level=0):
    if not os.path.isfile('cscope.out'):
        print("Please run: cscope -Rcbk", file=sys.stderr)
        return False
    if printed is None:
        printed = set()
    if node in printed:
        print_limited(level*'\t' + node + ' ^')
        return
    else:
        print_limited(level*'\t' + node)
    printed.add(node)
    if level > level_limit - 2:
        print_limited((level + 1)*'\t' + '...')
        return ''
    local_printed = set()
    for line in popen('cscope -d -L2 "%s"' % (node)):
        a = line.split()[1]
        if a in local_printed or a in black_list:
            continue
        local_printed.add(a)
        # try:
        call_tree(line.split()[1], printed, level + 1)
        # except Exception:
        #    pass
    return ''


def call_dep(node, printed=None, level=0):
    if not os.path.isfile('cscope.out'):
        print("Please run: cscope -Rcbk", file=sys.stderr)
        return False
    if printed is None:
        printed = set()
    if node in printed:
        return
    calls = list()
    for a in [line.split()[1] for line in
              popen('cscope -d -L2 "%s"' % (node))]:
        if a in black_list:
            continue
        calls.append(a)
    if calls:
        if level < level_limit - 1:
            printed.add(node)
            print("%s:" % (node), ' '.join(list(dict.fromkeys(calls))))
            for a in list(dict.fromkeys(calls)):
                call_dep(a, printed, level + 1)
        else:
            pass
            # TODO: print terminal
            # print('...')
    return ''


def my_graph(name=None):
    g = nx.DiGraph(name=name)
    # g.graph.update({'node': {'shape': 'none', 'fontsize': 50}})
    # g.graph.update({'rankdir': 'LR', 'nodesep': 0, })
    return g


def reduce_graph(g, m=None):
    rm = set()
    m = g.number_of_nodes() if not m else m
    log(g.number_of_edges())
    rm = [n for (n, d) in g.out_degree if not d and g.in_degree(n) <= m]
    g.remove_nodes_from(rm)
    print(g.number_of_edges())
    return g


def includes(a):
    res = []
    # log(a)
    for a in popen('man -s 2 %s 2> /dev/null |'
                   ' head -n 20 | grep include || true' % (a)):
        m = re.match('.*<(.*)>', a)
        if m:
            res.append(m.group(1))
    if not res:
        for a in popen('grep -l -r " %s *(" '
                       '/usr/include --include "*.h" '
                       '2> /dev/null || true' % (a)):
            # log(a)
            a = re.sub(r'.*/(bits)', r'\1', a)
            a = re.sub(r'.*/(sys)', r'\1', a)
            a = re.sub(r'/usr/include/(.*)', r'\1', a)
            # log(a)
            res.append(a)
    res = set(res)
    if res and len(res) > 1:
        r = set()
        for f in res:
            # log('grep " %s \+\(" --include "%s" -r /usr/include/'%(a, f))
            # log(os.system(
            # 'grep -w "%s" --include "%s" -r /usr/include/'%(a, f)))
            if 0 != os.system(
                    'grep " %s *(" --include "%s" -r /usr/include/ -q'
                    % (a, os.path.basename(f))):
                r.add(f)
        res = res.difference(r)
    log(res)
    return ','.join(list(res)) if res else 'unexported'


def syscalls():
    sc = my_graph('syscalls')
    inc = 'includes.list'
    if not os.path.isfile(inc):
        os.system('ctags --langmap=c:+.h --c-kinds=+pex -I __THROW '
                  + ' -R -u -f- /usr/include/ | cut -f1,2 > '
                  + inc)
    '''
   if False:
        includes = {}
        with open(inc, 'r') as f:
            for s in f:
                includes[s.split()[0]] = s.split()[1]
        log(includes)
    '''
    scd = 'SYSCALL_DEFINE.list'
    if not os.path.isfile(scd):
        os.system("grep SYSCALL_DEFINE -r --include='*.c' > " + scd)
    with open(scd, 'r') as f:
        v = set(['sigsuspend', 'llseek', 'sysfs',
                 'sync_file_range2', 'ustat', 'bdflush'])
        for s in f:
            if any(x in s.lower() for x in ['compat', 'stub']):
                continue
            m = re.match(r'(.*?):.*SYSCALL.*\(([\w]+)', s)
            if m:
                for p in {
                        '^old',
                        '^xnew',
                        r'.*64',
                        r'.*32$',
                        r'.*16$',
                }:
                    if re.match(p, m.group(2)):
                        m = None
                        break
                if m:
                    syscall = m.group(2)
                    syscall = re.sub('^new', '', syscall)
                    path = m.group(1).split('/')
                    if (m.group(1).startswith('mm/nommu.c')
                            or m.group(1).startswith('arch/x86/ia32')
                            or m.group(1).startswith('arch/')
                            or syscall.startswith('vm86')
                            and not m.group(1).startswith('arch/x86')):
                        continue
                    if syscall in v:
                        continue
                    v.add(syscall)
                    p2 = '/'.join(path[1:])
                    p2 = m.group(1)
                    # if log(difflib.get_close_matches(syscall, v) or ''):
                    #    log(syscall)
                    # log(syscall + ' ' + (includes.get(syscall) or '------'))
                    # man -s 2  timerfd_settime | head -n 20
                    if False:
                        i = includes(syscall)
                        log(p2 + ' ' + str(i) + ' ' + syscall)
                        sc.add_edge(i, i+' - '+p2)
                        sc.add_edge(i+' - '+p2, 'sys_' + syscall)
                    else:
                        sc.add_edge(path[0] + '/', p2)
                        sc.add_edge(p2, 'sys_' + syscall)
    return sc


# DiGraph
# write_dot to_agraph AGraph
# agwrite
# srcxray.py 'write_dot(syscalls(), "syscalls.dot")'
# srcxray.py "write_dot(import_cflow(), 'a.dot')"
# write_graphml
# F=sys_mount; srcxray.py "digraph_print(import_cflow(), ['$F'])" > $F.tree
# srcxray.py "leaves(read_dot('a.dot'))"
# srcxray.py "most_used(read_dot('a.dot'))"
# srcxray.py "digraph_print(read_dot('a.dot'))"
# srcxray.py "write_dot(reduce_graph(read_dot('no-loops.dot')),'reduced.dot')"
# a=sys_clone;srcxray.py "write_dot(rank_couples(reduce_graph(remove_loops(read_dot('$a.dot')))),'$a.dot')"
# srcxray.py "pprint(most_used(read_dot('a.dot')))"
# srcxray.py "write_dot(digraph_tree(read_dot('all.dot'), ['sys_clone']), 'sys_clone.dot')"
# srcxray.py "write_dot(add_rank('reduced.dot'), 'ranked.dot')"
# srcxray.py "write_dot(remove_loops(read_dot('reduced.dot')), 'no-loops.dot')"

def cleanup(a):
    log('')
    g = to_dg(a)
    print(dg.number_of_edges())
    dg.remove_nodes_from(black_list)
    print(dg.number_of_edges())
    write_dot(dg, a)


def leaves(a):
    dg = to_dg(a)
    log(dg)
    # [x for x in G.nodes() if G.out_degree(x)==0 and G.in_degree(x)==1]
    return {n: dg.in_degree(n) for (n, d) in dg.out_degree if not d}


def sort_dict(d):
    return [a for a, b in sorted(d.items(), key=lambda k: k[1], reverse=True)]


def most_used(dg, ins=10, outs=10):
    # return {a: b for a, b in sorted(dg.in_degree, key=lambda k: k[1]) if b > 1 and}
    # return [(x, dg.in_degree(x), dg.out_degree(x))
    return [x
            for x in dg.nodes()
            if dg.in_degree(x) >= ins and dg.out_degree(x) >= outs]


def starts(dg):  # roots
    return {n: dg.out_degree(n) for (n, d) in dg.in_degree if not d}


def digraph_tree(dg, starts=None, black_list=black_list):
    tree = nx.DiGraph()

    def sub(node):
        tree.add_node(node)
        for o in dg.successors(node):
            if o in black_list or tree.has_edge(node, o) or o in starts:
                # print(o)
                continue
            tree.add_edge(node, o)
            sub(o)

    printed = set()
    if not starts:
        starts = {}
        for i in [n for (n, d) in dg.in_degree if not d]:
            starts[i] = dg.out_degree(i)
        starts = [a[0] for a in sorted(
            starts.items(), key=lambda k: k[1], reverse=True)]
    if len(starts) == 1:
        sub(starts[0])
    elif len(starts) > 1:
        for o in starts:
            if o in black_list:
                continue
            sub(o)
    return tree


def digraph_print(dg, starts=None, dst_fn=None, sort=False):
    dst = open(dst_fn, 'w') if dst_fn else None
    printed = set()

    def digraph_print_sub(path='', node=None, level=0):
        if node in black_list:
            return
        if node in printed:
            print_limited(level*'\t' + str(node) + ' ^', dst)
            return
        outs = {_: dg.out_degree(_) for _ in dg.successors(node)}
        if sort:
            outs = {a: b for a, b in sorted(
                outs.items(), key=lambda k: k[1], reverse=True)}
        s = ''
        if 'rank' in dg.nodes[node]:
            s = str(dg.nodes[node]['rank'])
            ranks[dg.nodes[node]['rank']].append(node)
        if outs:
            s += ' ...' if level > level_limit - 2 else '  @' + path
        print_limited(level*'\t' + str(node) + s, dst)
        printed.add(node)
        if level > level_limit - 2:
            return ''
        passed = set()
        for o in outs.keys():
            if o in passed:
                continue
            passed.add(o)
            digraph_print_sub(path + ' ' + str(node), o, level + 1)

    if not starts:
        starts = {}
        for i in [n for (n, d) in dg.in_degree if not d]:
            starts[i] = dg.out_degree(i)
        starts = [a[0] for a in sorted(
            starts.items(), key=lambda k: k[1], reverse=True)]
    if len(starts) > 1:
        print_limited(default_root, dst)
        for s in starts:
            print_limited('\t' + s + ' ->', dst)
    passed = set()
    for o in starts:
        if o in passed:
            continue
        passed.add(o)
        if o in dg:
            digraph_print_sub('', o)
    #  not yet printed rest:
    for o in dg.nodes():
        if o not in printed:
            digraph_print_sub('', o)
    if dst_fn:
        print(dst_fn)
        dst.close()


def cflow_preprocess(a):
    with open(a, 'rb') as f:
        for s in f:
            try:
                s = s.decode('utf-8')
            except UnicodeDecodeError:
                s = s.decode('latin1')
            if cflow_structs:
                # treat structs like functions
                s = re.sub(r"^static struct (\w+) = ", r"\1()", s)
                s = re.sub(r"^static struct (\w+)\[\] = ", r"\1()", s)
                s = re.sub(r"^static const struct (\w+)\[\] = ", r"\1()", s)
                s = re.sub(r"^struct (.*) =", r"\1()", s)
            s = re.sub(r"^static __initdata int \(\*actions\[\]\)\(void\) = ",
                       "int actions()", s)  # init/initramfs.c
            s = re.sub(r"^static ", "", s)
            s = re.sub(r"SENSOR_DEVICE_ATTR.*\((\w*),",
                       r"void sensor_dev_attr_\1()(", s)
            s = re.sub(r"COMPAT_SYSCALL_DEFINE[0-9]\((\w*),",
                       r"compat_sys_\1(", s)
            s = re.sub(r"SYSCALL_DEFINE[0-9]\((\w*)", r"sys_\1(", s)
            s = re.sub(r"__setup\(.*,(.*)\)", r"void __setup() {\1();}", s)
            s = re.sub(r"^(\w*)param\(.*,(.*)\)", r"void \1param() {\2();}", s)
            s = re.sub(r"^(\w*)initcall\((.*)\)",
                       r"void \1initcall() {\2();}", s)
            s = re.sub(r"^static ", "", s)
            s = re.sub(r"^inline ", "", s)
            s = re.sub(r"^const ", "", s)
            s = re.sub(r"\b__initdata\b", "", s)
            s = re.sub(r"DEFINE_PER_CPU\((.*),(.*)\)", r"\1 \2", s)
            s = re.sub(r"^(\w+) {$", r"void \1() {", s)
            # for line in sys.stdin:
            sys.stdout.write(s)


cflow_param = {
    "modifier": "__init __inline__ noinline __initdata __randomize_layout asmlinkage "
                " __visible __init __leaf__ __ref __latent_entropy __init_or_module  libmosq_EXPORT",
                "wrapper": "__attribute__ __section__ "
                "TRACE_EVENT MODULE_AUTHOR MODULE_DESCRIPTION MODULE_LICENSE MODULE_LICENSE MODULE_SOFTDEP "
                "INIT_THREAD_INFO "
                "__acquires __releases __ATTR"
                # "wrapper": "__setup early_param"
}

# export CPATH=:include:arch/x86/include:../build/include/:../build/arch/x86/include/generated/:include/uapi
# srcxray.py "'\n'.join(cflow('init/main.c'))"


def cflow(a=None):
    if os.path.isfile('include/linux/cache.h'):
        for m in popen("ctags -x --c-kinds=d include/linux/cache.h | cut -d' '  -f 1 | sort -u"):
            if m in cflow_param['modifier']:
                print(m)
            else:
                cflow_param['modifier'] += ' ' + a
    if not a:
        a = "$(cat cscope.files)" if os.path.isfile(
            'cscope.files') else "*.c *.h *.cpp *.hh "
    elif isinstance(a, list):
        pass
    elif os.path.isdir(a):
        a = "$(find {0} -name '*.[ch]' -o -name '*.cpp' -o -name '*.hh')".format(a)
        pass
    elif os.path.isfile(a):
        pass
    # "--depth=%d " %(level_limit+1) +
    # --debug=1
    cflow = (r"cflow -m _ignore_main_get_all_ -v "
             # + "-DCONFIG_KALLSYMSZ "
             + "--preprocess='srcxray.py cflow_preprocess' "
             + ''.join([''.join(["--symbol={0}:{1} ".format(w, p)
                                 for w in cflow_param[p].split()])
                        for p in cflow_param.keys()])
             + " --include=_sxt --brief --level-indent='0=\t' "
             + a)
    log(cflow)
    return popen(cflow)


def import_cflow(a=None, cflow_out=None):
    cf = my_graph()
    stack = list()
    nprev = -1
    cflow_out = open(cflow_out, 'w') if cflow_out else None
    for line in cflow(a):
        if cflow_out:
            cflow_out.write(line + '\n')
        # --print-level
        m = re.match(r'^([\t]*)([^(^ ^<]+)', str(line))
        if m:
            n = len(m.group(1))
            id = str(m.group(2))
        else:
            raise Exception(line)

        if n <= nprev:
            stack = stack[:n - nprev - 1]
        # print(n, id, stack)
        if id not in black_list:
            if len(stack):
                cf.add_edge(stack[-1], id)
        stack.append(id)
        nprev = n
    return cf


def import_outline(a=None):
    cf = my_graph()
    stack = list()
    nprev = -1
    with open(a, 'r') as f:
        for line in f:
            m = re.match(r'^([\t ]*)(.*)', str(line))
            if m:
                n = len(m.group(1))
                id = str(m.group(2))
            else:
                raise Exception(line)
            if not id:
                continue
            id = re.sub(' \^$', '', id)
            if n <= nprev:
                stack = stack[:n - nprev - 1]
            # print(n, id, stack)
            if id not in black_list:
                if len(stack):
                    cf.add_edge(stack[-1], id)
            stack.append(id)
            nprev = n
    return cf


def rank(g, n):
    try:
        if g.nodes[n]['rank1'] == g.nodes[n]['rank2']:
            return g.nodes[n]['rank1']
        if g.nodes[n]['rank1'] < abs(g.nodes[n]['rank2']):
            return g.nodes[n]['rank1']
        else:
            return g.__dict__['max_rank'] + 1 + g.nodes[n]['rank2']
    except KeyError:
        return None


def write_dot(g, dot):
    dot = str(dot)
    dot = open(dot, 'w')
    dot.write('strict digraph "None" {\n')
    dot.write('rankdir=LR\nnodesep=0\n')
    # dot.write('ranksep=50\n')
    dot.write('node [fontname=Ubuntu,shape=none];\n')
    # dot.write('edge [width=10000];\n')
    dot.write('edge [width=1];\n')
    g.remove_nodes_from(black_list)
    ranks = collections.defaultdict(list)
    for n in g.nodes():
        r = rank(g, n)
        if r:
            ranks[r].append(n)
        if not g.out_degree(n):
            continue
        dot.write('"%s" -> { ' % re.escape(n))
        dot.write(' '.join(['"%s"' % (re.escape(str(a)))
                            for a in g.successors(n)]))
        if scaled and r and int(r):
            dot.write(' } [penwidth=%d label=%d];\n' % (100/r, r))
        else:
            dot.write(' } ;\n')
    print(ranks.keys())
    for r in ranks.keys():
        dot.write("{ rank=same %s }\n" %
                  (' '.join(['"%s"' % (str(a)) for a in ranks[r]])))
    for n in g.nodes():
        prop = Munch(g.node[n])
        if scaled and len(ranks):
            prop.fontsize = 500 + 10000 / (len(ranks[rank(g, n)]) + 1)
        prop.fontsize = 30 + min(5 * len(g.edges(n)), 50)

        # prop.label = n + ' ' + str(rank(g,n))
        if prop:
            dot.write('"%s" [%s]\n' % (re.escape(n), ','.join(
                ['%s="%s"' % (a, str(prop[a])) for a in prop])))
        elif not g.number_of_edges():
            dot.write('"%s"\n' % (n))
        # else:
        #    dot.write('"%s"\n'%(n))
    dot.write('}\n')
    dot.close()
    print(dot.name)


@open_file(0, mode='r')
def read_dot(dot):
    # read_dot pydot.graph_from_dot_data parse_dot_data from_pydot
    dg = nx.DiGraph()
    for a in dot:
        a = a.strip()
        if '->' in a:
            m = re.match('"?([^"]+)"? -> {(.+)}', a)
            if m:
                dg.add_edges_from([(m.group(1), b.strip('"'))
                                   for b in m.group(2).split() if b != m.group(1)])
            else:
                m = re.match('"?([^"]+)"? -> "?([^"]*)"?;', a)
                if m:
                    if m.group(1) != m.group(2):
                        dg.add_edge(m.group(1), m.group(2))
                else:
                    log(a)
        elif re.match('.*[=\[\]{}]', a):
            continue
        else:
            m = re.match('"?([^"]+)"?', a)
            if m:
                if m.group(1):
                    dg.add_node(m.group(1))
    return dg


def to_dg(a):
    if isinstance(a, nx.DiGraph):
        log(a)
        return a
    if os.path.isfile(a):
        log(a)
        return read_dot(a)
    raise(Exception(a))


def remove_loops(dg):
    rm = []
    visited = set()
    path = [object()]
    path_set = set(path)
    stack = [iter(dg)]
    while stack:
        for v in stack[-1]:
            if v in path_set:
                rm.append((path[-1], v))
            elif v not in visited:
                visited.add(v)
                path.append(v)
                path_set.add(v)
                stack.append(iter(dg[v]))
                break
        else:
            path_set.remove(path.pop())
            stack.pop()
    # print(rm)
    dg.remove_edges_from(rm)
    return dg


def remove_couples(dg):
    couples = []
    for n in dg:
        if dg.out_degree(n) == 1:
            s = list(dg.successors(n))[0]
            if dg.in_degree(s) == 1:
                couples.append((n, s))
    pprint(couples)
    dg.remove_edges_from(couples)
    return dg


def rank_couples(dg):
    couples = []
    ranked = set()
    for n in dg:
        if n in ranked:
            continue
        m = n
        while True:
            if dg.out_degree(m) == 1:
                s = list(dg.successors(m))[0]
                if dg.in_degree(s) == 1:
                    couples.append((m, s))
                ranked.update(set((m, s)))
                dg.nodes[m]['rank1'] = dg.nodes[m]['rank2'] = dg.nodes[s]['rank1'] = dg.nodes[s]['rank2'] = n
                m = s
                continue
            break
    return dg


def cflow_dir(a):
    index = nx.DiGraph()
    for c in glob.glob(os.path.join(a, "*.c")):
        g = None
        dot = str(Path(c).with_suffix(".dot"))
        if not os.path.isfile(dot):
            # c -> cflow and dot
            g = import_cflow(c, Path(c).with_suffix(".cflow"))
            write_dot(g, dot)
            print(dot, popen("ctags -x %s | wc -l" % (c))
                  [0], len(set(e[0] for e in g.edges())))
        else:
            print(dot)
            try:
                # g = nx.drawing.nx_agraph.read_dot(dot)
                g = read_dot(dot)
            except (TypeError, pygraphviz.agraph.DotError):
                print('nx_pydot <- nx_agraph')
                g = nx.drawing.nx_pydot.read_dot(dot)
        # digraph_print(g, [], Path(c).with_suffix(".tree"))
        # index.add_nodes_from(g.nodes())
        index.add_edges_from(g.edges())
    write_dot(index, str(os.path.join(a, 'index.dot')))
    digraph_print(digraph_tree(index), [], os.path.join(a, 'index.tree'))
    return index


def cflow_linux():
    dirs = ('init kernel kernel/time '
            'fs fs/ext4 block '
            'ipc net '
            'lib security security/keys '
            'arch/x86/kernel drivers/char drivers/pci '
            ).split()

    # dirs += ('mm net/ipv4 crypto').split()
    dirs = ('init kernel arch/x86/kernel fs ').split()
    dirs += ['mm']

    # fs/notify/fanotify fs/notify/inotify
    all = None
    try:
        print('loading all.dot')
        all = read_dot('all.dot')
        # all = nx.DiGraph(read_dot('all.dot'))
    except FileNotFoundError:
        pass
    if not all:
        all = nx.DiGraph()
        for a in dirs:
            print(a)
            index = cflow_dir(a)
            # all.add_nodes_from(index.nodes())
            all.add_edges_from(index.edges())
        write_dot(all, 'all.dot')
    remove_loops(all)
    print('loops: ' + str(list(all.nodes_with_selfloops())))
    print('trees:')
    digraph_print(all, ['x86_64_start_kernel', 'start_kernel', 'main', 'initcall', 'early_param',
                        '__setup', 'sys_write', 'write'],
                  'all.tree')
    start_kernel = digraph_tree(all, ['start_kernel'])
    write_dot(start_kernel, 'start_kernel.dot')
    write_dot(reduce_graph(start_kernel), 'start_kernel-reduced.dot')
    write_dot(reduce_graph(reduce_graph(start_kernel)),
              'start_kernel-reduced2.dot')
    write_dot(reduce_graph(digraph_tree(all, ['sys_clone'])), 'sys_clone.dot')


def stats(a):
    dg = to_dg(a)
    stat = Munch()
    im = dict()
    om = dict()
    leaves = set()
    roots = dict()
    stat.edge_nodes = 0
    stat.couples = 0
    for n in dg:
        id = dg.in_degree(n)
        od = dg.out_degree(n)
        if id == 1 and od == 1:
            stat.edge_nodes += 1
        if id:
            im[n] = id
        else:
            roots[n] = od
        if od:
            om[n] = od
        else:
            leaves.add(n)
        if od == 1 and dg.in_degree(list(dg.successors(n))[0]) == 1:
            stat.couples += 1
    stat.max_in_degree = max(dict(dg.in_degree).values())
    stat.max_out_degree = max(dict(dg.out_degree).values())
    stat.leaves = len(leaves)
    stat.roots = len(roots)
    stat.big_roots = ' '.join(sort_dict(roots)[:20])
    # pprint(im)
    # pprint(om)
    stat._popular = ' '.join(sort_dict(im)[:10])
    stat._biggest = ' '.join(sort_dict(om)[:10])
    gd = remove_loops(dg)
    stat.dag_longest_path_len = len(dag_longest_path(dg))
    stat.__longest_path = ' '.join(dag_longest_path(dg)[:10] + [''])
    for a in [nx.DiGraph.number_of_nodes, nx.DiGraph.number_of_edges, nx.DiGraph.number_of_selfloops,
              nx.DiGraph.order]:
        stat[a.__name__] = a(dg)
    pprint(dict(stat))


def dot_expand(a, b):
    a = to_dg(a)
    b = to_dg(b)
    c = my_graph()
    log(a.nodes())
    c.add_edges_from(b.out_edges(b.nbunch_iter(a.nodes())))
    print(list(b.nbunch_iter(a.nodes())))
    return c


def add_rank(g):
    g = to_dg(g)
    passed1 = set()
    passed2 = set()
    rn1 = 1
    rn2 = -1
    r1 = [n for (n, d) in g.in_degree if not d]
    r2 = [n for (n, d) in g.out_degree if not d]
    while r1 or r2:
        if r1:
            nxt = set()
            for n in r1:
                g.nodes[n]['rank1'] = max(rn1, g.nodes[n].get('rank1', rn1))
                for i in [_ for _ in g.successors(n)]:
                    nxt.add(i)
                    passed1.add(i)
            rn1 += 1
            r1 = nxt
        if r2:
            nxt = set()
            for n in r2:
                g.nodes[n]['rank2'] = min(rn2, g.nodes[n].get('rank2', rn2))
                for i in [_ for _ in g.predecessors(n)]:
                    nxt.add(i)
                    passed2.add(i)
            rn2 -= 1
            r2 = nxt
    g.__dict__['max_rank'] = rn1
    return g


def import_symbols():
    sym = my_graph('symbols')
    for l in popen('(shopt -s globstar;  nm -D -C -A **/*.so.*)'):
        q = l.split(maxsplit=2)
        m = re.match(r'.*lib(.+).so.*:.*', q[0])
        if not m:
            log(q[0])
            continue
        if q[1] == 'U':
            sym.add_edge(m.group(1), q[2])
        elif q[1] == 'T':
            sym.add_edge(q[2], m.group(1))
        print(m.group(1), q[1], q[2])
    return sym


me = os.path.basename(sys.argv[0])


def dir_tree(d='.'):
    stack = list()
    nprev = -1
    g = my_graph()
    # all = nx.DiGraph()
    # TODO
    for path, dirs, files, fds in os.fwalk(d):
        (dir, base) = os.path.split(path)
        dir = re.sub(r'^\.\/', '', dir)
        path = re.sub(r'^\.\/', '', path)
        path2 = path.split(os.sep)
        # print(path, fds, len(path2))
        if re.match(r'\.repo/', path) or len(path2) > level_limit:
            # print("skip", path)
            continue
        if len(path2) > 1:
            # g.add_edge(path2[-2] + str(), path2[-1])
            if g.number_of_edges() > limit:
                g.add_edge(dir, '...')
                break
            g.add_edge(dir, path)
            #g.add_node(path, label=path2[-1], xlabel='<<font point-size="1">'+path+'</font>>')
            g.add_node(path, label=path2[-1])
            #g.add_node(path, label=path2[-1], xlabel=path)
    print(g.number_of_edges())
    return g


def doxygen_xml(a):
    g = my_graph()
    for x in list(glob.glob(os.path.join(a, "*.xml")) + [a]):
        if os.path.isfile(x):
            d = xml.dom.minidom.parse(x)
            for m in d.getElementsByTagName("memberdef"):
                n = m.getElementsByTagName("name")[0].firstChild.data
                for r in m.getElementsByTagName("references"):
                    g.add_edge(n, r.firstChild.data)
                # referencedby
    return g


def doxygen_length(a):
    g = my_graph()
    for x in list(glob.glob(os.path.join(a, "*.xml")) + [a]):
        if os.path.isfile(x):
            d = xml.dom.minidom.parse(x)
            for m in d.getElementsByTagName("memberdef"):
                n = m.getElementsByTagName("name")[0].firstChild.data
                location = m.getElementsByTagName("location")[0]
                # for r in m.getElementsByTagName("references"):
                #    g.add_edge(n, r.firstChild.data)
                # referencedby
                e = location.getAttribute('bodyend')
                if not e or e == "-1":
                    continue
                l = int(e) - int(location.getAttribute('bodystart'))
                if l < 20:
                    continue
                print(location.getAttribute('bodystart'), n, location.getAttribute('file'), location.getAttribute('bodyfile'), x, file=sys.stderr)
                print("{0}:{1}:".format(location.getAttribute('bodyfile'),
                                        location.getAttribute('bodystart')), n, l, "SLOC")
                #<location file="common/log.cpp" line="21" column="1" bodyfile="common/log.cpp" bodystart="21" bodyend="49"/>
    return g


def usage():
    print("Usage:\n")
    for c in ["referers_tree", "call_tree", "referers_dep", "call_dep"]:
        print(me, c, "<identifier>")
    print("\nTry this:\n")
    print("cd linux/init")
    print(me, "unittest")
    print(me, "referers_tree nfs_root_data")
    print(me, "call_tree start_kernel")
    print(me, "import_cflow $none_or_dir_or_file_or_mask")
    print(me, "stats $dot")
    print(me, "leaves $dot")
    print(me, "cflow_linux")
    print(me, "\"write_dot(import_outline('outline.txt'),'outline.dot')\"")
    print(me, "\"write_dot(dir_tree('.'),'tree.dot')\"")
    print(me, "\"write_dot(doxygen_xml('xml/'),'doxygen.dot')\"")
    print("Emergency termination: ^Z, kill %1")
    print()


class _unittest_autotest(unittest.TestCase):
    def test_1(self):
        write_dot(nx.DiGraph([(1, 2), (2, 3), (2, 4)]), 'test.dot')
        g = read_dot("test.dot")
        self.assertEqual(list(g.successors("2")), ["3", "4"])
        self.assertTrue(os.path.isdir('include/linux/'))
        os.chdir('init')
        self.assertEqual('\t\t\t\t\tprepare_namespace ^', popen(
            'srcxray.py referers_tree nfs_root_data')[-1])
        self.assertEqual('initrd_load: prepare_namespace', popen(
            'srcxray.py referers_dep nfs_root_data')[-1])
        self.assertEqual('calibrate_delay_converge: __delay',
                         popen('srcxray.py call_dep start_kernel')[-2])
        self.assertEqual('\t\tcpu_startup_entry', popen(
            'srcxray.py call_tree start_kernel')[-1])
        os.chdir('..')
        self.assertTrue(syscalls().number_of_edges() > 400)
        # digraph_print:
        self.assertEqual("\t\tmount_initrd ^", popen(
            "srcxray.py import_cflow init/do_mounts_initrd.c")[-1])
        self.assertEqual("\t\t4", popen(
            'srcxray.py "nx.DiGraph([{1,2},{2,3},{2,4}])"')[-1])


def main():
    try:
        ret = False
        if len(sys.argv) == 1:
            print('Run', me, 'usage')
        else:
            a1 = sys.argv[1]
            sys.argv = sys.argv[1:]
            if '(' in a1:
                ret = eval(a1)
                # ret = exec(sys.argv[1])
            elif len(sys.argv) == 1 and isinstance(eval(a1), types.ModuleType):
                ret = eval(a1 + ".main()")
            else:
                ret = eval(a1 + '(' + ', '.join("'%s'" % (a)
                                                for a in sys.argv[1:]) + ')')
        if isinstance(ret, nx.DiGraph):
            digraph_print(ret)
        elif isinstance(ret, bool) and ret is False:
            sys.exit(os.EX_CONFIG)
        else:
            if (ret is not None):
                print(ret)
    except KeyboardInterrupt:
        log("\nInterrupted")


if __name__ == "__main__":
    main()
