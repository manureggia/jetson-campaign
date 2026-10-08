#define _GNU_SOURCE
#include <linux/bpf.h>
#include <linux/perf_event.h>
#include <sys/syscall.h>
#include <sys/ioctl.h>
#include <sys/resource.h>
#include <sys/utsname.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <time.h>

/* Build-specific probe; no BTF, compiler backend or libbpf required.
 * Instructions below are ordinary UAPI eBPF instructions, checked by verifier.
 * Gate operand is ARM64 pt_regs.regs[0], at byte offset zero. */
struct metric { uint64_t n, sum, min, max; };
struct state { uint64_t start, active, reason, nested, read_errors, backwards;
               struct metric result[2]; uint64_t gate_errors, end_errors; int64_t last_error; };
static char verifier[65536];
static int dump_only;
static int attached[2]={-1,-1}, made[2];
static volatile sig_atomic_t interrupted;
static const char *names[]={"gate","end"};
static const char *trace="/sys/kernel/tracing";
static void stop(int sig) { (void)sig; interrupted=1; }
static void fail(const char *what) { perror(what); exit(1); }
static void cleanup(void) {
    for(int i=0;i<2;i++) if(attached[i]>=0) close(attached[i]);
    for(int i=1;i>=0;i--) if(made[i]) {
        char path[256],line[128]; snprintf(path,sizeof(path),"%s/kprobe_events",trace);
        int fd=open(path,O_WRONLY|O_APPEND); if(fd>=0) {
            int n=snprintf(line,sizeof(line),"-:ni_pmu/%s\n",names[i]);
            if(write(fd,line,n)!=n) perror("cleanup kprobe");
            close(fd);
        }
    }
}
static int bpf_call(enum bpf_cmd cmd, union bpf_attr *a) {
    return syscall(__NR_bpf,cmd,a,sizeof(*a));
}
static int map_create(int type, int value_size) {
    union bpf_attr a={0};a.map_type=type;a.key_size=4;a.value_size=value_size;a.max_entries=1;
    int fd=bpf_call(BPF_MAP_CREATE,&a);if(fd<0)fail("BPF_MAP_CREATE");return fd;
}
static void update(int fd, void *value) {
    uint32_t key=0;union bpf_attr a={0};a.map_fd=fd;a.key=(uintptr_t)&key;a.value=(uintptr_t)value;
    if(bpf_call(BPF_MAP_UPDATE_ELEM,&a))fail("BPF_MAP_UPDATE_ELEM");
}
/* Tiny assembler: jump targets are patched by label, avoiding numeric offsets. */
struct program { struct bpf_insn code[160]; int n, label[16], fixpos[80], fixto[80], fixes; };
static void emit(struct program *p,int op,int dst,int src,int off,int imm) {
    if(p->n>=160){fprintf(stderr,"BPF program too large\n");exit(1);}
    p->code[p->n++]=(struct bpf_insn){.code=op,.dst_reg=dst,.src_reg=src,.off=off,.imm=imm};
}
#define MOV(p,d,s) emit(p,BPF_ALU64|BPF_MOV|BPF_X,d,s,0,0)
#define IMM(p,d,v) emit(p,BPF_ALU64|BPF_MOV|BPF_K,d,0,0,v)
#define LOAD(p,d,s,o) emit(p,BPF_LDX|BPF_MEM|BPF_DW,d,s,o,0)
#define STORE(p,d,s,o) emit(p,BPF_STX|BPF_MEM|BPF_DW,d,s,o,0)
#define ADD(p,d,v) emit(p,BPF_ALU64|BPF_ADD|BPF_K,d,0,0,v)
#define CALL(p,id) emit(p,BPF_JMP|BPF_CALL,0,0,0,id)
#define O(member) ((int)offsetof(struct state,member))
static void label(struct program*p,int l){p->label[l]=p->n;}
static void jump(struct program*p,int op,int dst,int src,int imm,int to){
    p->fixpos[p->fixes]=p->n;p->fixto[p->fixes++]=to;emit(p,op,dst,src,0,imm);
}
#define JEQ(p,d,v,l) jump(p,BPF_JMP|BPF_JEQ|BPF_K,d,0,v,l)
#define JNE(p,d,v,l) jump(p,BPF_JMP|BPF_JNE|BPF_K,d,0,v,l)
#define JA(p,l) jump(p,BPF_JMP|BPF_JA,0,0,0,l)
static void map_ptr(struct program*p,int reg,int fd){
    emit(p,BPF_LD|BPF_DW|BPF_IMM,reg,BPF_PSEUDO_MAP_FD,0,fd);emit(p,0,0,0,0,0);
}
static void increment(struct program*p,int offset){LOAD(p,0,7,offset);ADD(p,0,1);STORE(p,7,0,offset);}
/* Labels: 0=exit, 1=inactive/normal, 2=readOK, 3=reasonReady, 4=minDone,
 * 5=maxDone, 6=metricReady, 7=counterOK. */
static int load_program(int statefd,int counterfd,int controlfd,int tid,int ending,unsigned kernel_version){
    struct program p={0};for(int i=0;i<16;i++)p.label[i]=-1;
    MOV(&p,6,1);CALL(&p,BPF_FUNC_get_current_pid_tgid);
    emit(&p,BPF_ALU|BPF_MOV|BPF_X,0,0,0,0); /* low 32 bits = TID */
    JNE(&p,0,tid,0);
    emit(&p,BPF_ST|BPF_MEM|BPF_W,10,0,-4,0);
    /* Trace-call BPF executes before perf's stopped-state check: explicit
     * arming is required even when the tracepoint perf FD is disabled. */
    if(!ending){
        map_ptr(&p,1,controlfd);MOV(&p,2,10);ADD(&p,2,-4);CALL(&p,BPF_FUNC_map_lookup_elem);
        JEQ(&p,0,0,0);emit(&p,BPF_LDX|BPF_MEM|BPF_W,0,0,0,0);JEQ(&p,0,0,0);
    }
    map_ptr(&p,1,statefd);MOV(&p,2,10);ADD(&p,2,-4);CALL(&p,BPF_FUNC_map_lookup_elem);
    JEQ(&p,0,0,0);MOV(&p,7,0);LOAD(&p,0,7,O(active));
    if(ending){JEQ(&p,0,0,0);IMM(&p,0,0);STORE(&p,7,0,O(active));}
    else {JEQ(&p,0,0,1);increment(&p,O(nested));JA(&p,0);label(&p,1);
        /* Read the exact branch operand; normalize to 0/1. */
        emit(&p,BPF_LDX|BPF_MEM|BPF_W,8,6,0,0);JEQ(&p,8,0,3);IMM(&p,8,1);label(&p,3);
        STORE(&p,7,8,O(reason));}
    map_ptr(&p,1,counterfd);IMM(&p,2,0);MOV(&p,3,10);ADD(&p,3,-32);IMM(&p,4,24);
    CALL(&p,BPF_FUNC_perf_event_read_value);JEQ(&p,0,0,2);
    STORE(&p,7,0,O(last_error));increment(&p,ending?O(end_errors):O(gate_errors));
    increment(&p,O(read_errors));JA(&p,0);
    label(&p,2);LOAD(&p,8,10,-32);
    if(!ending){STORE(&p,7,8,O(start));IMM(&p,0,1);STORE(&p,7,0,O(active));}
    else {
        LOAD(&p,9,7,O(start));
        jump(&p,BPF_JMP|BPF_JGE|BPF_X,8,9,0,7);increment(&p,O(backwards));JA(&p,0);label(&p,7);
        emit(&p,BPF_ALU64|BPF_SUB|BPF_X,8,9,0,0);
        LOAD(&p,0,7,O(reason));JEQ(&p,0,0,6);ADD(&p,7,sizeof(struct metric));label(&p,6);ADD(&p,7,O(result));
        LOAD(&p,0,7,0);ADD(&p,0,1);STORE(&p,7,0,0);
        LOAD(&p,0,7,8);emit(&p,BPF_ALU64|BPF_ADD|BPF_X,0,8,0,0);STORE(&p,7,0,8);
        LOAD(&p,0,7,16);jump(&p,BPF_JMP|BPF_JGE|BPF_X,8,0,0,4);STORE(&p,7,8,16);label(&p,4);
        LOAD(&p,0,7,24);jump(&p,BPF_JMP|BPF_JLE|BPF_X,8,0,0,5);STORE(&p,7,8,24);label(&p,5);
    }
    label(&p,0);IMM(&p,0,0);emit(&p,BPF_JMP|BPF_EXIT,0,0,0,0);
    for(int i=0;i<p.fixes;i++){
        int dest=p.label[p.fixto[i]];if(dest<0){fprintf(stderr,"Unresolved BPF label\n");exit(1);}
        p.code[p.fixpos[i]].off=dest-p.fixpos[i]-1;
    }
    if(dump_only){
        printf("%s\"%s\":[",ending?",":"",ending?"end":"gate");
        for(int i=0;i<p.n;i++)printf("%s[%u,%u,%u,%d,%d]",i?",":"",p.code[i].code,p.code[i].dst_reg,p.code[i].src_reg,p.code[i].off,p.code[i].imm);
        printf("]");return p.n;
    }
    union bpf_attr a={0};a.prog_type=BPF_PROG_TYPE_KPROBE;a.insn_cnt=p.n;a.insns=(uintptr_t)p.code;
    a.license=(uintptr_t)"GPL";a.kern_version=kernel_version;a.log_buf=(uintptr_t)verifier;
    a.log_size=sizeof(verifier);a.log_level=1;
    int fd=bpf_call(BPF_PROG_LOAD,&a);if(fd<0){fprintf(stderr,"%s\n",verifier);fail("BPF_PROG_LOAD");}return fd;
}
static int perf_open(struct perf_event_attr*a,int tid,int cpu){
    int fd=syscall(__NR_perf_event_open,a,tid,cpu,-1,PERF_FLAG_FD_CLOEXEC);
    if(fd<0)fail("perf_event_open");
    return fd;
}
static void attach(int i,int program){
    char path[256],definition[256];snprintf(path,sizeof(path),"%s/events/ni_pmu/%s",trace,names[i]);
    if(access(path,F_OK)==0){fprintf(stderr,"Event ni_pmu/%s already exists; stop its owner first.\n",names[i]);exit(1);}
    snprintf(path,sizeof(path),"%s/kprobe_events",trace);int fd=open(path,O_WRONLY|O_APPEND);if(fd<0)fail(path);
    int n=snprintf(definition,sizeof(definition),i==0?"p:ni_pmu/gate newidle_balance.constprop.0+156\n":"r128:ni_pmu/end newidle_balance.constprop.0\n");
    if(write(fd,definition,n)!=n)fail("register kprobe");
    close(fd);made[i]=1;
    snprintf(path,sizeof(path),"%s/events/ni_pmu/%s/id",trace,names[i]);FILE*f=fopen(path,"r");int id;
    if(!f || fscanf(f,"%d",&id)!=1)fail("read kprobe id");
    fclose(f);
    struct perf_event_attr a={.size=sizeof(a),.type=PERF_TYPE_TRACEPOINT,.config=id,.sample_period=1,.disabled=1};
    attached[i]=perf_open(&a,-1,0);
    if(ioctl(attached[i],PERF_EVENT_IOC_SET_BPF,program))fail("PERF_EVENT_IOC_SET_BPF");
}
static unsigned long long misses(void){
    char path[256],line[512],name[128];unsigned long long hits,miss,total=0;snprintf(path,sizeof(path),"%s/kprobe_profile",trace);
    FILE*f=fopen(path,"r");if(!f)fail(path);
    while(fgets(line,sizeof(line),f)) if(sscanf(line,"%127s %llu %llu",name,&hits,&miss)==3 &&
        (!strcmp(name,"gate")||!strcmp(name,"end")))total+=miss;
    fclose(f);return total;
}
static double elapsed(struct timespec a,struct timespec b){return b.tv_sec-a.tv_sec+(b.tv_nsec-a.tv_nsec)/1e9;}
int main(int argc,char**argv){
    if(argc==2&&!strcmp(argv[1],"dump")){
        dump_only=1;printf("{");load_program(1,2,3,123,0,0);load_program(1,2,3,123,1,0);printf("}\n");return 0;
    }
    int measure=argc==4&&!strcmp(argv[1],"measure"),count=argc==4&&!strcmp(argv[1],"count");
    if(!measure&&!count){fprintf(stderr,"Usage: newidle-pmu measure|count TID SECONDS\n");return 2;}
    char*end;long tid=strtol(argv[2],&end,10);if(*end||tid<1||tid>2147483647L)return 2;
    long seconds=strtol(argv[3],&end,10);if(*end||seconds<1||seconds>120)return 2;
    if(geteuid()){fprintf(stderr,"Run with sudo.\n");return 1;}
    /* Protect the build-specific offset, before any tracing changes. */
    if(system("printf '%s  /sys/kernel/notes\\n' e09c7f29fe0eabd63df26ade5734c070fdf58d3b9b3a8ee020b8d9ca33ecdaf4 | sha256sum --status -c -")){
        fprintf(stderr,"Kernel notes mismatch\n");return 1;}
    char path[128],line[256];snprintf(path,sizeof(path),"/proc/%ld/status",tid);FILE*f=fopen(path,"r");int pinned=0;
    if(!f)fail(path);
    while(fgets(line,sizeof(line),f))if(!strncmp(line,"Cpus_allowed_list:",18)){
        char cpus[128];if(sscanf(line+18,"%127s",cpus)==1&&!strcmp(cpus,"0"))pinned=1;
    }fclose(f);if(!pinned){fprintf(stderr,"Worker must be pinned exclusively to CPU 0\n");return 1;}
    struct rlimit limit={RLIM_INFINITY,RLIM_INFINITY};if(setrlimit(RLIMIT_MEMLOCK,&limit))fail("memlock");
    atexit(cleanup);signal(SIGINT,stop);signal(SIGTERM,stop);
    struct perf_event_attr a={.size=sizeof(a),.type=PERF_TYPE_HARDWARE,.config=PERF_COUNT_HW_INSTRUCTIONS,
        .disabled=1,.pinned=1,.exclude_user=1,.exclude_hv=1,
        .read_format=PERF_FORMAT_TOTAL_TIME_ENABLED|PERF_FORMAT_TOTAL_TIME_RUNNING};
    int counter=perf_open(&a,tid,0),segment_counter=-1,statefd=-1,controlfd=-1;struct state state={0};unsigned long long before=0;
    if(measure){
        /* Always-on CPU counter avoids per-task perf scheduling transitions.
         * No task switch can occur inside the measured IRQ/preempt-disabled
         * newidle segment. The separate task counter still measures totals. */
        segment_counter=perf_open(&a,-1,0);
        controlfd=map_create(BPF_MAP_TYPE_ARRAY,4);uint32_t armed=0;update(controlfd,&armed);
        struct utsname u;unsigned major,minor,patch;if(uname(&u)||sscanf(u.release,"%u.%u.%u",&major,&minor,&patch)!=3)fail("uname");
        int pmufd=map_create(BPF_MAP_TYPE_PERF_EVENT_ARRAY,4);statefd=map_create(BPF_MAP_TYPE_ARRAY,sizeof(state));
        state.result[0].min=state.result[1].min=UINT64_MAX;update(statefd,&state);update(pmufd,&segment_counter);
        unsigned version=(major<<16)|(minor<<8)|patch;
        int progs[2]={load_program(statefd,pmufd,controlfd,tid,0,version),load_program(statefd,pmufd,controlfd,tid,1,version)};
        attach(0,progs[0]);attach(1,progs[1]);before=misses();
    }
    if(measure&&(ioctl(segment_counter,PERF_EVENT_IOC_RESET,0)||ioctl(segment_counter,PERF_EVENT_IOC_ENABLE,0)))fail("enable CPU counter");
    if(ioctl(counter,PERF_EVENT_IOC_RESET,0)||ioctl(counter,PERF_EVENT_IOC_ENABLE,0))fail("enable counter");
    struct timespec t0,t1,req={seconds,0};clock_gettime(CLOCK_MONOTONIC,&t0);
    if(measure){
        if(ioctl(attached[1],PERF_EVENT_IOC_ENABLE,0))fail("enable end");
        if(ioctl(attached[0],PERF_EVENT_IOC_ENABLE,0))fail("enable gate");
        uint32_t armed=1;update(controlfd,&armed);
    }
    while(nanosleep(&req,&req)&&errno==EINTR&&!interrupted){}
    if(measure){
        uint32_t armed=0;update(controlfd,&armed);
        if(ioctl(attached[0],PERF_EVENT_IOC_DISABLE,0))fail("disable gate");
        /* Drain an in-flight return while the counter remains enabled. */
        struct timespec drain={0,2000000};nanosleep(&drain,NULL);
        if(ioctl(attached[1],PERF_EVENT_IOC_DISABLE,0))fail("disable end");
        /* Detach BPF before disabling PMU, not merely stop perf events. */
        close(attached[0]);attached[0]=-1;close(attached[1]);attached[1]=-1;
    }
    if(ioctl(counter,PERF_EVENT_IOC_DISABLE,0))fail("disable counter");
    clock_gettime(CLOCK_MONOTONIC,&t1);
    uint64_t values[3];if(read(counter,values,sizeof(values))!=sizeof(values))fail("read counter (pinned event unavailable?)");
    uint64_t segment_values[3]={0};
    if(measure){
        if(ioctl(segment_counter,PERF_EVENT_IOC_DISABLE,0))fail("disable CPU counter");
        if(read(segment_counter,segment_values,sizeof(segment_values))!=sizeof(segment_values))fail("read CPU counter");
    }
    unsigned long long lost=0;
    if(measure){uint32_t key=0;union bpf_attr b={0};b.map_fd=statefd;b.key=(uintptr_t)&key;b.value=(uintptr_t)&state;
        if(bpf_call(BPF_MAP_LOOKUP_ELEM,&b))fail("read state");
        lost=misses()-before;}
    int valid=!interrupted&&values[1]&&values[1]==values[2]&&!lost&&!state.active&&!state.nested&&!state.read_errors&&!state.backwards&&(!measure||(state.result[0].n+state.result[1].n>0&&segment_values[1]&&segment_values[1]==segment_values[2]));
    printf("{\"mode\":\"%s\",\"tid\":%ld,\"seconds\":%.9f,\"instructions\":%llu,\"enabled_ns\":%llu,\"running_ns\":%llu,\"valid\":%s,\"misses\":%llu,\"active\":%llu,\"nested\":%llu,\"read_errors\":%llu,\"backwards\":%llu,\"segments\":[",
        argv[1],tid,elapsed(t0,t1),(unsigned long long)values[0],(unsigned long long)values[1],(unsigned long long)values[2],valid?"true":"false",lost,(unsigned long long)state.active,(unsigned long long)state.nested,(unsigned long long)state.read_errors,(unsigned long long)state.backwards);
    for(int i=0;i<2;i++)printf("%s{\"overload\":%d,\"calls\":%llu,\"sum\":%llu,\"min\":%llu,\"max\":%llu}",i?",":"",i,(unsigned long long)state.result[i].n,(unsigned long long)state.result[i].sum,(unsigned long long)(state.result[i].n?state.result[i].min:0),(unsigned long long)state.result[i].max);
    printf("],\"segment_counter_scope\":\"CPU0 kernel, filtered boundaries for TID\",\"segment_enabled_ns\":%llu,\"segment_running_ns\":%llu,\"gate_read_errors\":%llu,\"end_read_errors\":%llu,\"last_read_errno\":%lld}\n",(unsigned long long)segment_values[1],(unsigned long long)segment_values[2],(unsigned long long)state.gate_errors,(unsigned long long)state.end_errors,(long long)state.last_error);
    return valid?0:1;
}
