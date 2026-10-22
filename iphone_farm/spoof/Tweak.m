// rnxspoof — per-container device identity, PLAIN ObjC runtime only (no MSHookFunction).
// Reads /var/mobile/Library/Preferences/com.reynex.spoof.plist and returns the values there.
// "apps" is a bundle-id allow-list; processes not in it stay untouched.
//
// 2026-10-17 iteration 2: dropped the MGCopyAnswer MSHookFunction hook entirely. On iOS 16.7 with
// ElleKit, hooking MGCopyAnswer leaves a `brk 1` at the libMobileGestalt entry with no reachable
// handler -- every IG launch crashes within 2s before the hook body even runs (not a Copy-semantic
// issue after all; CFBridgingRetain fix in iteration 1 did not help). What Instagram actually reads
// via PUBLIC iOS APIs on iOS 7+ is IDFV + IDFA; UDID/Serial/IMEI/CarrierName are gated behind private
// MG keys IG wouldn't dare ship a dependency on. So the ObjC method-swizzles of UIDevice +
// ASIdentifierManager carry the whole win, with none of the low-level hook risk.

#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
#import <AdSupport/AdSupport.h>
#import <objc/runtime.h>

#define PLIST @"/var/mobile/Library/Preferences/com.reynex.spoof.plist"

static NSDictionary *P = nil;
static BOOL ACTIVE = NO;
static NSString *val(NSString *k) { return [P[k] isKindOfClass:[NSString class]] ? P[k] : nil; }

@implementation UIDevice (RnxSpoof)
- (NSUUID *)rnx_identifierForVendor {
    if (!ACTIVE) return [self rnx_identifierForVendor];
    NSString *v = val(@"idfv");
    return v ? [[NSUUID alloc] initWithUUIDString:v] : [self rnx_identifierForVendor];
}
- (NSString *)rnx_name { return ACTIVE ? @"iPhone" : [self rnx_name]; }
@end

@implementation ASIdentifierManager (RnxSpoof)
- (NSUUID *)rnx_advertisingIdentifier {
    if (!ACTIVE) return [self rnx_advertisingIdentifier];
    NSString *v = val(@"idfa");
    return v ? [[NSUUID alloc] initWithUUIDString:v] : [self rnx_advertisingIdentifier];
}
@end

static void swap(Class c, SEL a, SEL b) {
    Method m1 = class_getInstanceMethod(c, a);
    Method m2 = class_getInstanceMethod(c, b);
    if (m1 && m2) method_exchangeImplementations(m1, m2);
}

__attribute__((constructor))
static void rnx_init(void) {
    // 2026-10-17 iteration 3 (lastExceptionBacktrace from Instagram-2026-10-17-143151.ips):
    // val -> UIDevice rnx_identifierForVendor crashed with an "unrecognized selector" exception
    // because P was a dangling pointer. Root cause: this file is compiled without -fobjc-arc
    // (plain MRC), so `P = [NSDictionary dictionaryWithContentsOfFile:...]` assigned an
    // AUTORELEASED object to a static -- the next pool drain freed it. Fix: retain P explicitly
    // (MRC) and move the swizzle out of the autoreleasepool so none of the stored references
    // can get dropped behind our back. The autoreleasepool is still useful for the local vars
    // read during init, but P must outlive it.
    @autoreleasepool {
        NSDictionary *loaded = [NSDictionary dictionaryWithContentsOfFile:PLIST] ?: @{};
        P = [loaded retain];                                 // MRC: keep P alive beyond this pool
        NSArray *apps = P[@"apps"];
        if ([apps isKindOfClass:[NSArray class]]) {
            NSString *me = [[NSBundle mainBundle] bundleIdentifier] ?: @"";
            ACTIVE = [apps containsObject:me];
        }
    }
    swap([UIDevice class], @selector(identifierForVendor), @selector(rnx_identifierForVendor));
    swap([UIDevice class], @selector(name),                @selector(rnx_name));
    swap([ASIdentifierManager class], @selector(advertisingIdentifier), @selector(rnx_advertisingIdentifier));
}
