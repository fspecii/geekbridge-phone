// SPDX-License-Identifier: GPL-3.0-or-later
// The system's voice-processing audio unit: built-in microphone and speakers with echo
// cancellation, noise suppression and automatic gain. Mono 16-bit at the rate the unit accepts.
#import <AudioToolbox/AudioToolbox.h>
#import <Foundation/Foundation.h>
#include <stdlib.h>
#include <string.h>

typedef void (*gbp_capture_cb)(void *user, const int16_t *samples, uint32_t count);
typedef void (*gbp_render_cb)(void *user, int16_t *samples, uint32_t count);

typedef struct {
    AudioUnit unit;
    gbp_capture_cb capture;
    gbp_render_cb render;
    void *user;
    int16_t *scratch;
    uint32_t scratch_frames;
} gbp_vpio;

static OSStatus input_cb(void *ref, AudioUnitRenderActionFlags *flags, const AudioTimeStamp *ts,
                         UInt32 bus, UInt32 frames, AudioBufferList *unused) {
    (void)bus; (void)unused;
    gbp_vpio *v = ref;
    if (frames > v->scratch_frames) return noErr;
    AudioBufferList list;
    list.mNumberBuffers = 1;
    list.mBuffers[0].mNumberChannels = 1;
    list.mBuffers[0].mDataByteSize = frames * 2;
    list.mBuffers[0].mData = v->scratch;
    OSStatus st = AudioUnitRender(v->unit, flags, ts, 1, frames, &list);
    if (st == noErr) v->capture(v->user, v->scratch, frames);
    return noErr;
}

static OSStatus render_cb(void *ref, AudioUnitRenderActionFlags *flags, const AudioTimeStamp *ts,
                          UInt32 bus, UInt32 frames, AudioBufferList *list) {
    (void)flags; (void)ts; (void)bus;
    gbp_vpio *v = ref;
    int16_t *out = list->mBuffers[0].mData;
    v->render(v->user, out, frames);
    return noErr;
}

static OSStatus try_rate(gbp_vpio *v, Float64 rate) {
    AudioStreamBasicDescription f = {0};
    f.mSampleRate = rate;
    f.mFormatID = kAudioFormatLinearPCM;
    f.mFormatFlags = kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked;
    f.mFramesPerPacket = 1;
    f.mChannelsPerFrame = 1;
    f.mBitsPerChannel = 16;
    f.mBytesPerFrame = 2;
    f.mBytesPerPacket = 2;
    OSStatus st = AudioUnitSetProperty(v->unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output, 1, &f, sizeof f);
    if (st != noErr) return st;
    return AudioUnitSetProperty(v->unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 0, &f, sizeof f);
}

void gbp_vpio_stop(void *handle) {
    gbp_vpio *v = handle;
    if (!v) return;
    if (v->unit) {
        AudioOutputUnitStop(v->unit);
        AudioUnitUninitialize(v->unit);
        AudioComponentInstanceDispose(v->unit);
    }
    free(v->scratch);
    free(v);
}

// Returns a handle, or NULL with a reason in err. `*actual` is the rate in use.
void *gbp_vpio_start(gbp_capture_cb capture, gbp_render_cb render, void *user, uint32_t *actual, char *err, size_t en) {
    gbp_vpio *v = calloc(1, sizeof *v);
    v->capture = capture; v->render = render; v->user = user;
    v->scratch_frames = 4096;
    v->scratch = calloc(v->scratch_frames, 2);
    AudioComponentDescription d = {0};
    d.componentType = kAudioUnitType_Output;
    d.componentSubType = kAudioUnitSubType_VoiceProcessingIO;
    d.componentManufacturer = kAudioUnitManufacturer_Apple;
    AudioComponent comp = AudioComponentFindNext(NULL, &d);
    OSStatus st = comp ? AudioComponentInstanceNew(comp, &v->unit) : -1;
    if (st != noErr) { snprintf(err, en, "no voice-processing audio unit (%d)", (int)st); gbp_vpio_stop(v); return NULL; }
    UInt32 on = 1;
    AudioUnitSetProperty(v->unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &on, sizeof on);
    AudioUnitSetProperty(v->unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &on, sizeof on);
    static const Float64 rates[] = {8000, 16000, 24000, 48000};
    Float64 chosen = 0;
    for (int i = 0; i < 4 && !chosen; i++) if (try_rate(v, rates[i]) == noErr) chosen = rates[i];
    if (!chosen) { snprintf(err, en, "the audio unit accepts none of the sample rates"); gbp_vpio_stop(v); return NULL; }
    AURenderCallbackStruct cb = { input_cb, v };
    st = AudioUnitSetProperty(v->unit, kAudioOutputUnitProperty_SetInputCallback, kAudioUnitScope_Global, 0, &cb, sizeof cb);
    AURenderCallbackStruct rcb = { render_cb, v };
    if (st == noErr) st = AudioUnitSetProperty(v->unit, kAudioUnitProperty_SetRenderCallback, kAudioUnitScope_Input, 0, &rcb, sizeof rcb);
    if (st == noErr) st = AudioUnitInitialize(v->unit);
    if (st == noErr) st = AudioOutputUnitStart(v->unit);
    if (st != noErr) { snprintf(err, en, "the audio unit would not start (%d)", (int)st); gbp_vpio_stop(v); return NULL; }
    *actual = (uint32_t)chosen;
    return v;
}
