"""Exercise the actual generated BPF bytecode with simulated perf helper reads."""
import json
from pathlib import Path
import subprocess
programs=json.loads(subprocess.check_output([str(Path(__file__).with_name('newidle-pmu')), 'dump']))
BASE=4096
CONTROL=12288
memory={BASE+i:0 for i in range(0,136,8)}
memory[CONTROL]=1
memory[BASE+64]=memory[BASE+96]=(1<<64)-1

def run(name,counter,operand=0,tid=123,read_error=0):
    r=[0]*11;r[1]=8192;r[10]=16384
    mem=dict(memory);mem[8192]=operand
    pc=0;steps=0
    code=programs[name]
    while True:
        steps+=1;assert steps<1000
        op,dst,src,off,imm=code[pc];pc+=1
        if op==0x18: # pseudo map fd
            r[dst]=imm;pc+=1
        elif op in (0xbf,0xb7,0xbc):
            r[dst]=r[src] if op!=0xb7 else imm
            if op==0xbc:r[dst]&=0xffffffff
        elif op in (0x07,0x0f,0x1f):
            val=imm if op==0x07 else r[src]
            r[dst]=(r[dst]-val if op==0x1f else r[dst]+val)&((1<<64)-1)
        elif op in (0x79,0x61):
            r[dst]=mem.get(r[src]+off,0)
            if op==0x61:r[dst]&=0xffffffff
        elif op in (0x7b,0x62):
            mem[r[dst]+off]=r[src] if op==0x7b else imm
        elif op==0x85:
            if imm==14:r[0]=(999<<32)|tid
            elif imm==1:
                assert r[1] in (1,3) and mem[r[2]]==0
                r[0]=BASE if r[1]==1 else CONTROL
            elif imm==55:
                assert r[1]==2 and r[2]==0 and r[4]==24
                r[0]=read_error
                mem[r[3]]=counter;mem[r[3]+8]=100;mem[r[3]+16]=100
            else:raise AssertionError(('unknown helper',imm))
            for reg in range(1,6):r[reg]=0
        elif op==0x95:
            assert r[0]==0
            for addr in memory:memory[addr]=mem[addr]
            return
        elif op in (0x05,0x15,0x55,0x3d,0xbd):
            take=(op==0x05 or op==0x15 and r[dst]==imm or
                  op==0x55 and r[dst]!=imm or op==0x3d and r[dst]>=r[src] or
                  op==0xbd and r[dst]<=r[src])
            if take:pc+=off
            assert 0<=pc<len(code)
        else:raise AssertionError(('unknown opcode',hex(op)))

memory[CONTROL]=0
old=dict(memory);run('gate',0,read_error=-16);assert memory==old
memory[CONTROL]=1
run('end',0) # no gate in this invocation
run('gate',10000,operand=0);assert memory[BASE+8]==1
memory[CONTROL]=0
run('end',18000) # pending return must drain even with capture disarmed
memory[CONTROL]=1
assert [memory[BASE+o] for o in (48,56,64,72)]==[1,8000,8000,8000]
run('gate',20000,operand=7);assert memory[BASE+16]==1
run('end',35000)
assert [memory[BASE+o] for o in (80,88,96,104)]==[1,15000,15000,15000]
old=dict(memory);run('gate',50000,tid=124);assert memory==old
run('gate',50000);run('gate',51000);assert memory[BASE+24]==1
run('end',55000)
assert [memory[BASE+o] for o in (48,56,64,72)]==[2,13000,5000,8000]
run('gate',60000);run('end',61000,read_error=-22)
assert memory[BASE+8]==0 and memory[BASE+32]==1
assert memory[BASE+120]==1 and memory[BASE+128]==-22
run('gate',70000);run('end',69000)
assert memory[BASE+40]==1
print('OK: explicit arming/disarming and pending return drain; bytecode gate/return, TID filter, classes, min/max/sum, nesting, read failure, backwards counter')
