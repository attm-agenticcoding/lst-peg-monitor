// Known-state cash sensitivity, derived from the independently cross-checked
// October 2026 mechanism. Fulu/Electra v1.6.0. Does not predict future rewards.
#include <algorithm>
#include <cstdint>
#include <deque>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>
using namespace std;
using U = uint64_t;
const U G=1000000000ULL, FAR=UINT64_MAX, MINB=32*G, MAXB=2048*G;
struct V {U bal,eff,ex,we,act; uint8_t prefix,slashed,vault;};
struct D {int64_t idx; U prefix,vault,amt,slot; string pk;};
struct PP {U i,a,e;};
struct C {U s,t;};
void check(bool b,const string& message){if(!b)throw runtime_error(message);}
U add(U a,U b){check(b<=FAR-a,"uint64 overflow");return a+b;}
U u64(const char* p){U x=0;for(int i=7;i>=0;i--)x=(x<<8)|static_cast<unsigned char>(p[i]);return x;}
void readat(ifstream& f,U at,char* out,size_t length){f.seekg(at);f.read(out,length);check(bool(f),"truncated SSZ input");}
U maxb(const V&v){return v.prefix==2?MAXB:MINB;}
bool executable(const V&v){return v.prefix==1||v.prefix==2;}
bool needs_hysteresis(const V&v){return add(v.bal,G/4)<v.eff || add(v.eff,5*G/4)<v.bal;}
void hysteresis(V&v){if(needs_hysteresis(v))v.eff=min(v.bal-v.bal%G,maxb(v));}
bool recipient(const char* credentials){
 const unsigned char vault[20]={0xb9,0xd7,0x93,0x48,0x78,0xb5,0xfb,0x96,0x10,0xb3,0xfe,0x8a,0x5e,0x44,0x1e,0x8f,0xad,0x7e,0x29,0x3f};
 if(credentials[0]!=1&&credentials[0]!=2)return false;
 for(int i=0;i<20;i++)if(static_cast<unsigned char>(credentials[12+i])!=vault[i])return false;
 return true;
}
int run(int argc,char**argv){
 check(argc==4,"usage: beacon-simulate STATE_SSZ CONFIG MODE");
 const string mode=argv[3];check(mode=="legacy"||mode=="legacy_reserved8"||mode=="quiescent","unsupported mode");
 const bool repeat=mode!="quiescent",reserve=mode=="legacy_reserved8";
 ifstream cfg(argv[2]);
 U startslot,genesis,initN,pointer,wi,depbal,vs,bs,ds_start,ds_count,ps_start,ps_count,cs_start,cs_count;
 cfg>>startslot>>genesis>>initN>>pointer>>wi>>depbal>>vs>>bs>>ds_start>>ds_count>>ps_start>>ps_count>>cs_start>>cs_count;
 check(bool(cfg)&&initN>0&&pointer<initN,"invalid model configuration");
 U refs_count;cfg>>refs_count;check(bool(cfg)&&refs_count>0&&refs_count<=10000,"invalid report count");
 vector<U> refs(refs_count),slots(refs_count);
 for(U i=0;i<refs_count;i++){
  cfg>>refs[i];check(bool(cfg)&&refs[i]>=genesis+startslot*12,"invalid reference timestamp");
  check(i==0||refs[i]>refs[i-1],"report references not increasing");slots[i]=(refs[i]-genesis)/12;
 }
 check(slots.back()-startslot<=31*7200,"model horizon too long");
 ifstream state(argv[1],ios::binary);check(bool(state),"cannot open state");
 // Only pending-deposit pubkeys are indexed, avoiding a multi-million-entry map.
 deque<D> ds;unordered_map<string,int64_t> wanted;
 char deposit[192];
 for(U i=0;i<ds_count;i++){
  readat(state,ds_start+i*192,deposit,192);string pk(deposit,48);
  U prefix=static_cast<unsigned char>(deposit[48]);
  U slot=u64(deposit+184);check(slot<=startslot,"pending deposit from after snapshot");
  ds.push_back(D{-1,prefix,U(recipient(deposit+48)),u64(deposit+80),slot,pk});wanted.emplace(pk,-1);
 }
 vector<V> v;v.reserve(initN+ds_count);char rec[121];
 U active_ejection_count=0,pending_slashed_count=0;
 state.seekg(vs);
 for(U i=0;i<initN;i++){
  state.read(rec,121);check(bool(state),"truncated SSZ validators");
  V a{0,u64(rec+80),u64(rec+105),u64(rec+113),u64(rec+97),
      static_cast<uint8_t>(rec[48]),static_cast<uint8_t>(rec[88]),static_cast<uint8_t>(recipient(rec+48))};
  check(a.slashed<=1,"invalid validator slashing flag");
  check(a.eff<=maxb(a)&&a.eff%G==0,"invalid validator effective balance");
  if(a.act<=startslot/32&&a.ex==FAR&&a.eff<=16*G)active_ejection_count++;
  auto it=wanted.find(string(rec,48));if(it!=wanted.end()){check(it->second<0,"duplicate validator pubkey");it->second=i;}
  v.push_back(a);
 }
 // Sequential balance reads, not a duplicate validators.bin or JSON artifact.
 state.seekg(bs);char amount[8];
 for(U i=0;i<initN;i++){
  state.read(amount,8);check(bool(state),"truncated SSZ balances");v[i].bal=u64(amount);
  if(v[i].slashed&&v[i].we>startslot/32)pending_slashed_count++;
 }
 check(active_ejection_count==0,"active unscheduled validator meets ejection threshold; forced exits unmodeled");
 check(pending_slashed_count==0,"slashed validator has future penalty/withdrawability exposure");
 for(auto& d:ds)d.idx=wanted.at(d.pk);
 wanted.clear();wanted.rehash(0);
 vector<PP> ps;char pp[24];for(U i=0;i<ps_count;i++){
  readat(state,ps_start+i*24,pp,24);PP p{u64(pp),u64(pp+8),u64(pp+16)};
  check(p.i<v.size(),"pending partial validator index out of range");ps.push_back(p);
 }
 vector<C> cs;char cc[16];for(U i=0;i<cs_count;i++){
  readat(state,cs_start+i*16,cc,16);C c{u64(cc),u64(cc+8)};
  check(c.s<v.size()&&c.t<v.size()&&c.s!=c.t,"consolidation validator index invalid");cs.push_back(c);
 }
 state.close();
 U ci=0,ppi=0,cash=0,fullcash=0,partialcash=0,pendingcash=0,synthetic=0,consolidated=0,applieddeps=0;
 unordered_map<string,U> newkeys;unordered_set<U> dirty;
 for(U i=0;i<v.size();i++)if(needs_hysteresis(v[i]))dirty.insert(i);
 auto withdrawal=[&](U i,U amount,int type){
  check(v[i].bal>=amount,"withdrawal exceeds balance");v[i].bal-=amount;dirty.insert(i);
  if(v[i].vault&&amount){cash=add(cash,amount);if(type==0)fullcash=add(fullcash,amount);else if(type==1)partialcash=add(partialcash,amount);else pendingcash=add(pendingcash,amount);}
  wi=add(wi,1);
 };
 auto applydep=[&](const D&d){
  int64_t idx=d.idx;if(idx<0){auto it=newkeys.find(d.pk);if(it!=newkeys.end())idx=it->second;}
  if(idx<0){idx=v.size();v.push_back(V{0,min(d.amt-d.amt%G,d.prefix==2?MAXB:MINB),FAR,FAR,FAR,static_cast<uint8_t>(d.prefix),0,static_cast<uint8_t>(d.vault)});newkeys[d.pk]=idx;}
  v[idx].bal=add(v[idx].bal,d.amt);dirty.insert(idx);applieddeps=add(applieddeps,d.amt);
 };
 cout<<"reference_timestamp,reference_slot,cumulative_cash_gwei,full_cash_gwei,partial_cash_gwei,pending_cash_gwei,pointer,registry_count,pending_deposits,pending_consolidations,synthetic_workload,consolidated_gwei,applied_deposits_gwei\n";
 U report_index=0;
 auto report=[&](U slot){
  while(report_index<refs.size()&&slots[report_index]==slot){
   check(add(add(fullcash,partialcash),pendingcash)==cash,"cash categories do not reconcile");
   cout<<refs[report_index]<<','<<slot<<','<<cash<<','<<fullcash<<','<<partialcash<<','<<pendingcash<<','<<pointer<<','<<v.size()<<','<<ds.size()<<','<<cs.size()-ci<<','<<synthetic<<','<<consolidated<<','<<applieddeps<<'\n';report_index++;
  }
 };
 report(startslot);
 for(U slot=startslot+1;slot<=slots.back();slot++){
  U epoch=slot/32;
  if(slot%32==0){
   // Epoch E starts after E-1 processing, with healthy finalized epoch E-3.
   U activebal=0;for(const auto&x:v)if(x.act<=epoch-1&&epoch-1<x.ex)activebal=add(activebal,x.eff);
   check(activebal/65536>=256*G,"activation/exit churn cap assumption no longer holds");
   U available=add(depbal,256*G),processed=0,count=0;bool limit=false;deque<D> postpone;
   while(!ds.empty()){
    D a=ds.front();if(a.slot>(epoch-3)*32||count>=16)break;
    int64_t idx=a.idx;if(idx<0){auto it=newkeys.find(a.pk);if(it!=newkeys.end())idx=it->second;}
    bool exited=idx>=0&&v[idx].ex<FAR,withdrawn=idx>=0&&v[idx].we<epoch;
    if(withdrawn)applydep(a);else if(exited)postpone.push_back(a);else{
     if(add(processed,a.amt)>available){limit=true;break;}processed=add(processed,a.amt);applydep(a);
    }
    ds.pop_front();count++;
   }
   while(!postpone.empty()){ds.push_back(postpone.front());postpone.pop_front();}
   depbal=limit?available-processed:0;
   while(ci<cs.size()){
    C c=cs[ci];V&s=v[c.s];if(s.slashed){ci++;continue;}if(s.we>epoch)break;
    U a=min(s.bal,s.eff);s.bal-=a;v[c.t].bal=add(v[c.t].bal,a);consolidated=add(consolidated,a);
    dirty.insert(c.s);dirty.insert(c.t);ci++;
   }
   for(U i:dirty){hysteresis(v[i]);check(!(v[i].act<=epoch&&v[i].ex==FAR&&v[i].eff<=16*G),"modeled active validator meets unmodeled ejection threshold");}
   dirty.clear();
  }
  U count=0,last=0;
  while(ppi<ps.size()&&count<8){
   PP p=ps[ppi];if(p.e>epoch)break;V&x=v[p.i];
   if(x.ex==FAR&&x.eff>=MINB&&x.bal>MINB){withdrawal(p.i,min(x.bal-MINB,p.a),2);last=p.i;count++;}
   ppi++;
  }
  if(reserve){synthetic=add(synthetic,8-count);wi=add(wi,8-count);count=8;}
  U idx=pointer,n=v.size(),scan=0;
  while(scan<min(n,U(16384))&&count<16){
   V&x=v[idx];U amount=0;int type=0;
   if(executable(x)&&x.we<=epoch&&x.bal>0){amount=x.bal;}
   else if(executable(x)&&x.eff==maxb(x)&&x.bal>maxb(x)){amount=x.bal-maxb(x);type=1;}
   if(amount){withdrawal(idx,amount,type);last=idx;count++;}
   else if(repeat&&x.prefix==1&&x.eff==MINB&&x.bal==MINB&&x.act<=epoch&&epoch<x.ex){
    // Workload only: synthetic one-gwei reward consumes capacity, never cash.
    last=idx;count++;synthetic=add(synthetic,1);wi=add(wi,1);
   }
   idx=(idx+1)%n;scan++;
  }
  pointer=count==16?(last+1)%v.size():(pointer+16384)%v.size();
  check(count<=16&&pointer<v.size(),"withdrawal capacity/pointer invariant violated");report(slot);
 }
 check(report_index==refs.size(),"missing simulation reports");
 return 0;
}
int main(int argc,char**argv){try{return run(argc,argv);}catch(const exception&e){cerr<<e.what()<<'\n';return 2;}}
