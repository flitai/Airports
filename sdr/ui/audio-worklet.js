// Airports · 本地接收机的音频播放
// SPDX-License-Identifier: GPL-3.0-only
//
// 守护进程每 20 毫秒发来一帧 16 kHz 的 PCM。网络上的帧不会正好每 20 毫秒到一帧，
// 而声卡要的是一刻不停的采样流，中间靠这个环形缓冲垫着。
//
// 规矩只有三条：
//   攒够 TARGET 才开始放——一开始就放的话，下一帧稍晚一点就断音。
//   放空了就停下来重新攒，而不是有一点放一点，那样听起来是连续的卡顿。
//   积压超过 MAX 就跳到只剩 TARGET——宁可丢半句，也不让延迟越积越大。
// 一次通话的第一帧带「开始」标志，见到它就清掉上一次通话的残余重新攒。
class RadioPlayer extends AudioWorkletProcessor {
  constructor(){
    super();
    this.size = 16000;                       // 环形缓冲 1 秒
    this.buf = new Float32Array(this.size);
    this.r = 0; this.w = 0; this.n = 0;      // 读指针、写指针、现有采样点数
    this.playing = false;
    this.TARGET = 1920;                      // 120 毫秒
    this.MAX = 6400;                         // 400 毫秒
    this.played = 0; this.underruns = 0; this.skipped = 0; this.tick = 0;
    this.port.onmessage = (e) => this.push(e.data);
  }
  push(m){
    if (m.reset){ this.r = this.w = this.n = 0; this.playing = false; return; }
    var pcm = m.pcm;
    if (m.start && !this.playing){ this.r = this.w; this.n = 0; }
    for (var i = 0; i < pcm.length; i++){
      this.buf[this.w] = pcm[i] / 32768;
      this.w = (this.w + 1) % this.size;
    }
    this.n = Math.min(this.size, this.n + pcm.length);
    if (this.n > this.MAX){                  // 积压太多：跳到只剩 TARGET
      var drop = this.n - this.TARGET;
      this.r = (this.r + drop) % this.size; this.n -= drop; this.skipped += drop;
    }
  }
  process(inputs, outputs){
    var out = outputs[0][0];
    if (!this.playing && this.n >= this.TARGET) this.playing = true;
    if (this.playing){
      var k = Math.min(out.length, this.n);
      for (var i = 0; i < k; i++){
        out[i] = this.buf[this.r];
        this.r = (this.r + 1) % this.size;
      }
      this.n -= k; this.played += k;
      if (k < out.length){ this.playing = false; this.underruns++; }   // 余下的保持为零
    }
    if (++this.tick % 50 === 0)              // 每 0.4 秒报一次，给界面和验收看
      this.port.postMessage({ played:this.played, buffered:this.n, underruns:this.underruns, skipped:this.skipped });
    return true;
  }
}
registerProcessor("radio-player", RadioPlayer);
