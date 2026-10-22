/*
 * rnxspoof — per-container device identity for the iPhone farm.
 *
 * Design: this tweak is DUMB on purpose. It reads a profile plist and returns
 * whatever identifiers that plist holds. ALL the logic -- which container maps
 * to which identity, and keeping it stable per container -- lives on the server
 * (iphone_farm_spoof.py), which writes the plist right before Instagram is
 * launched in a container (open_container_cli). One container open = one
 * identity written = a fresh Instagram process reads it on first access.
 *
 * Why a plain file read and not per-container logic in the tweak: Crane isolates
 * files/keychain per container but not these hardware IDs, and the tweak cannot
 * cleanly see "which Crane container am I in". The server already switches the
 * container, so it is the right place to decide the identity. Keeps the tweak
 * auditable (every value comes from one plist we control).
 *
 * Hooks the identifiers that otherwise tie all of a phone's containers together:
 * IDFV, IDFA, MobileGestalt UDID / Serial / IMEI, device name. DeviceCheck /
 * App Attest are Secure-Enclave backed and cannot be forged from userland; we do
 * NOT nil them by default (returning nil is itself a signal) -- the server can
 * turn that on per profile via the "nilAttest" key if a test ever shows it helps.
 *
 * Scope: Filter limits it to UIKit, i.e. normal apps. The server enables it per
 * app (same idea as Shadow) through the "apps" allow-list in the plist; an app
 * not in that list is left untouched so Shadowrocket / XXTouch are never spoofed.
 */

#import <UIKit/UIKit.h>
#import <Foundation/Foundation.h>
#import <AdSupport/AdSupport.h>
#include <dlfcn.h>

#define PLIST_PATH @"/var/mobile/Library/Preferences/com.reynex.spoof.plist"

static NSDictionary *_profile = nil;
static BOOL _active = NO;          // is this process one the server wants spoofed?

static NSDictionary *profile(void) {
    if (!_profile)
        _profile = [NSDictionary dictionaryWithContentsOfFile:PLIST_PATH] ?: @{};
    return _profile;
}

// the plist holds a top-level "apps" array of bundle ids the server wants spoofed;
// empty/missing array means "none", so the tweak is inert until the server opts an app in.
static BOOL wantsThisApp(void) {
    NSArray *apps = profile()[@"apps"];
    if (![apps isKindOfClass:[NSArray class]] || apps.count == 0) return NO;
    NSString *me = [[NSBundle mainBundle] bundleIdentifier] ?: @"";
    return [apps containsObject:me];
}

static NSString *val(NSString *key) { return profile()[key]; }

// ── UIDevice: IDFV + name/model ────────────────────────────────────────────
%hook UIDevice
- (NSUUID *)identifierForVendor {
    NSString *v = _active ? val(@"idfv") : nil;
    return v ? [[NSUUID alloc] initWithUUIDString:v] : %orig;
}
- (NSString *)name        { return _active ? @"iPhone" : %orig; }
%end

// ── IDFA ───────────────────────────────────────────────────────────────────
%hook ASIdentifierManager
- (NSUUID *)advertisingIdentifier {
    NSString *v = _active ? val(@"idfa") : nil;
    return v ? [[NSUUID alloc] initWithUUIDString:v] : %orig;
}
%end

// ── MobileGestalt: UDID / Serial / IMEI / device name ──────────────────────
typedef id (*MGCopyAnswerT)(CFStringRef key);
static MGCopyAnswerT orig_MGCopyAnswer = NULL;

static id hooked_MGCopyAnswer(CFStringRef key) {
    @autoreleasepool {
        if (_active && key) {
            NSString *k = (__bridge NSString *)key;
            if ([k isEqualToString:@"UniqueDeviceID"] || [k isEqualToString:@"UniqueDeviceIDData"]) {
                NSString *v = val(@"udid");
                if (v) return [v stringByReplacingOccurrencesOfString:@"-" withString:@""];
            } else if ([k isEqualToString:@"SerialNumber"]) {
                NSString *v = val(@"serial"); if (v) return v;
            } else if ([k isEqualToString:@"InternationalMobileEquipmentIdentity"]) {
                NSString *v = val(@"imei"); if (v) return v;
            } else if ([k isEqualToString:@"DeviceName"] || [k isEqualToString:@"UserAssignedDeviceName"]) {
                return @"iPhone";
            }
        }
    }
    return orig_MGCopyAnswer ? orig_MGCopyAnswer(key) : nil;
}

// ── optional: DeviceCheck / App Attest -> nil, only if the profile asks ─────
%group AttestNil
%hook DCDevice
- (void)generateTokenWithCompletionHandler:(void (^)(NSData *, NSError *))h {
    if (h) dispatch_async(dispatch_get_main_queue(), ^{ h(nil, nil); });
}
%end
%end

%ctor {
    // decide ONCE per process whether to spoof: only when the app opted in by the server.
    @autoreleasepool { _active = wantsThisApp(); }
    %init;
    if (_active && [profile()[@"nilAttest"] boolValue]) %init(AttestNil);

    void *mg = dlopen("/usr/lib/libMobileGestalt.dylib", RTLD_LAZY)
               ?: dlopen("/var/jb/usr/lib/libMobileGestalt.dylib", RTLD_LAZY);
    if (mg) {
        void *sym = dlsym(mg, "MGCopyAnswer");
        if (sym) MSHookFunction(sym, (void *)hooked_MGCopyAnswer, (void **)&orig_MGCopyAnswer);
    }
}
