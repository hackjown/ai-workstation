// AI Workstation — iOS app for the web console (TrollStore / unsigned).
// Full-screen WKWebView pointed at the API host. First launch asks for the URL,
// e.g. http://192.168.50.x:8000/?token=XXXX . Three-finger long-press to change it.
// Native bridge (window.webkit.messageHandlers.aiw): on-device dictation (Speech),
// read-aloud (AVSpeechSynthesizer), local notifications, haptics.
#import <UIKit/UIKit.h>
#import <WebKit/WebKit.h>
#import <Speech/Speech.h>
#import <AVFoundation/AVFoundation.h>
#import <UserNotifications/UserNotifications.h>

static NSString *const kURLKey = @"aiw_url";

@interface VC : UIViewController <WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler>
@property (nonatomic, strong) WKWebView *web;
@property (nonatomic, strong) AVAudioEngine *audio;
@property (nonatomic, strong) SFSpeechRecognizer *recognizer;
@property (nonatomic, strong) SFSpeechAudioBufferRecognitionRequest *speechReq;
@property (nonatomic, strong) SFSpeechRecognitionTask *speechTask;
@property (nonatomic, strong) AVSpeechSynthesizer *tts;
@end

@implementation VC
- (void)viewDidLoad {
    [super viewDidLoad];
    self.view.backgroundColor = [UIColor colorWithRed:0xFC/255.0 green:0xFC/255.0 blue:0xFC/255.0 alpha:1];
    WKWebViewConfiguration *cfg = [WKWebViewConfiguration new];
    [cfg.userContentController addScriptMessageHandler:self name:@"aiw"];
    cfg.allowsInlineMediaPlayback = YES;
    cfg.mediaTypesRequiringUserActionForPlayback = WKAudiovisualMediaTypeNone;
    cfg.websiteDataStore = [WKWebsiteDataStore defaultDataStore];
    self.web = [[WKWebView alloc] initWithFrame:self.view.bounds configuration:cfg];
    self.web.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
    self.web.navigationDelegate = self;
    self.web.UIDelegate = self;
    self.web.opaque = NO;
    self.web.allowsBackForwardNavigationGestures = YES;
    self.web.backgroundColor = self.view.backgroundColor;
    self.web.scrollView.contentInsetAdjustmentBehavior = UIScrollViewContentInsetAdjustmentNever;
    [self.view addSubview:self.web];

    UIRefreshControl *rc = [UIRefreshControl new];
    [rc addTarget:self action:@selector(reload:) forControlEvents:UIControlEventValueChanged];
    self.web.scrollView.refreshControl = rc;

    UILongPressGestureRecognizer *lp = [[UILongPressGestureRecognizer alloc] initWithTarget:self action:@selector(settings:)];
    lp.numberOfTouchesRequired = 3;
    [self.view addGestureRecognizer:lp];
}
- (void)viewDidAppear:(BOOL)animated {
    [super viewDidAppear:animated];
    static BOOL once = NO; if (once) return; once = YES;
    NSString *u = [[NSUserDefaults standardUserDefaults] stringForKey:kURLKey];
    if (u.length) [self open:u]; else [self askURL];
}
- (UIStatusBarStyle)preferredStatusBarStyle {
    if (@available(iOS 13.0, *)) return UIStatusBarStyleDarkContent;
    return UIStatusBarStyleDefault;
}

// ------------------------------------------------------------- native bridge
- (void)js:(NSString *)fn args:(NSArray *)args {
    NSData *d = [NSJSONSerialization dataWithJSONObject:args options:0 error:nil];
    NSString *a = [[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding];
    a = [a substringWithRange:NSMakeRange(1, a.length - 2)];
    NSString *code = [NSString stringWithFormat:@"window.%@ && window.%@(%@)", fn, fn, a];
    dispatch_async(dispatch_get_main_queue(), ^{ [self.web evaluateJavaScript:code completionHandler:nil]; });
}
- (void)userContentController:(WKUserContentController *)uc didReceiveScriptMessage:(WKScriptMessage *)msg {
    if (![msg.body isKindOfClass:NSDictionary.class]) return;
    NSDictionary *b = msg.body; NSString *op = b[@"op"];
    if ([op isEqualToString:@"dictate_start"]) [self startDictation];
    else if ([op isEqualToString:@"dictate_stop"]) [self stopDictation];
    else if ([op isEqualToString:@"speak"]) [self speak:b[@"text"]];
    else if ([op isEqualToString:@"notify"]) [self notifyTitle:b[@"title"] body:b[@"body"]];
    else if ([op isEqualToString:@"ready"]) {
        [[UNUserNotificationCenter currentNotificationCenter] requestAuthorizationWithOptions:
            (UNAuthorizationOptionAlert | UNAuthorizationOptionSound | UNAuthorizationOptionBadge) completionHandler:^(BOOL g, NSError *e) {}];
    }
}
- (void)startDictation {
    [SFSpeechRecognizer requestAuthorization:^(SFSpeechRecognizerAuthorizationStatus st) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if (st != SFSpeechRecognizerAuthorizationStatusAuthorized) { [self js:@"aiwDictationError" args:@[@"没有语音识别权限（设置 > AI Workstation）"]]; return; }
            [self beginRecording];
        });
    }];
}
- (void)beginRecording {
    [self stopDictation];
    self.recognizer = [[SFSpeechRecognizer alloc] initWithLocale:[NSLocale localeWithLocaleIdentifier:@"zh-CN"]];
    if (!self.recognizer.isAvailable) { [self js:@"aiwDictationError" args:@[@"语音识别暂不可用"]]; return; }
    NSError *err = nil;
    AVAudioSession *ses = [AVAudioSession sharedInstance];
    [ses setCategory:AVAudioSessionCategoryPlayAndRecord mode:AVAudioSessionModeMeasurement
             options:AVAudioSessionCategoryOptionDefaultToSpeaker | AVAudioSessionCategoryOptionDuckOthers error:&err];
    [ses setActive:YES withOptions:AVAudioSessionSetActiveOptionNotifyOthersOnDeactivation error:&err];
    self.audio = [AVAudioEngine new];
    self.speechReq = [SFSpeechAudioBufferRecognitionRequest new];
    self.speechReq.shouldReportPartialResults = YES;
    AVAudioInputNode *input = self.audio.inputNode;
    __weak typeof(self) ws = self;
    self.speechTask = [self.recognizer recognitionTaskWithRequest:self.speechReq resultHandler:^(SFSpeechRecognitionResult *r, NSError *e) {
        if (r) [ws js:@"aiwDictation" args:@[r.bestTranscription.formattedString ?: @"", @(r.isFinal)]];
        if (e || r.isFinal) {
            [ws.audio stop]; [ws.audio.inputNode removeTapOnBus:0];
            if (!r) [ws js:@"aiwDictation" args:@[@"", @YES]];
        }
    }];
    [input installTapOnBus:0 bufferSize:1024 format:[input outputFormatForBus:0] block:^(AVAudioPCMBuffer *buf, AVAudioTime *when) {
        [ws.speechReq appendAudioPCMBuffer:buf];
    }];
    [self.audio prepare];
    if (![self.audio startAndReturnError:&err]) { [self js:@"aiwDictationError" args:@[err.localizedDescription ?: @"麦克风不可用"]]; return; }
    [[[UIImpactFeedbackGenerator alloc] initWithStyle:UIImpactFeedbackStyleLight] impactOccurred];
}
- (void)stopDictation {
    if (self.audio.isRunning) { [self.audio stop]; [self.audio.inputNode removeTapOnBus:0]; }
    [self.speechReq endAudio];
    self.speechReq = nil;
}
- (void)speak:(NSString *)text {
    if (!text.length) return;
    if (!self.tts) self.tts = [AVSpeechSynthesizer new];
    [self.tts stopSpeakingAtBoundary:AVSpeechBoundaryImmediate];
    [[AVAudioSession sharedInstance] setCategory:AVAudioSessionCategoryPlayback error:nil];
    AVSpeechUtterance *u = [AVSpeechUtterance speechUtteranceWithString:text];
    u.voice = [AVSpeechSynthesisVoice voiceWithLanguage:@"zh-CN"];
    [self.tts speakUtterance:u];
}
- (void)notifyTitle:(NSString *)title body:(NSString *)body {
    [[UINotificationFeedbackGenerator new] notificationOccurred:UINotificationFeedbackTypeSuccess];
    if (UIApplication.sharedApplication.applicationState == UIApplicationStateActive) return;  // the page shows its own toast
    UNMutableNotificationContent *c = [UNMutableNotificationContent new];
    c.title = title ?: @"AI Workstation"; c.body = body ?: @""; c.sound = UNNotificationSound.defaultSound;
    UNNotificationRequest *r = [UNNotificationRequest requestWithIdentifier:NSUUID.UUID.UUIDString content:c trigger:nil];
    [[UNUserNotificationCenter currentNotificationCenter] addNotificationRequest:r withCompletionHandler:nil];
}
// let getUserMedia / file pickers work without a second prompt (iOS 15+)
- (void)webView:(WKWebView *)w requestMediaCapturePermissionForOrigin:(WKSecurityOrigin *)o initiatedByFrame:(WKFrameInfo *)f
           type:(WKMediaCaptureType)t decisionHandler:(void (^)(WKPermissionDecision))d {
    d(WKPermissionDecisionGrant);
}
- (void)reload:(UIRefreshControl *)rc { [self.web reload]; [rc endRefreshing]; }
- (void)open:(NSString *)u {
    NSURL *url = [NSURL URLWithString:u];
    if (url) [self.web loadRequest:[NSURLRequest requestWithURL:url]];
}
- (void)settings:(UIGestureRecognizer *)g { if (g.state == UIGestureRecognizerStateBegan) [self askURL]; }
- (void)askURL {
    UIAlertController *a = [UIAlertController alertControllerWithTitle:@"AI Workstation"
        message:@"API 地址，例如\nhttp://192.168.50.x:8000/?token=你的AIW_API_TOKEN\n（以后三指长按可修改）"
        preferredStyle:UIAlertControllerStyleAlert];
    [a addTextFieldWithConfigurationHandler:^(UITextField *t) {
        t.text = [[NSUserDefaults standardUserDefaults] stringForKey:kURLKey] ?: @"http://";
        t.keyboardType = UIKeyboardTypeURL;
        t.autocapitalizationType = UITextAutocapitalizationTypeNone;
        t.autocorrectionType = UITextAutocorrectionTypeNo;
        t.clearButtonMode = UITextFieldViewModeWhileEditing;
    }];
    [a addAction:[UIAlertAction actionWithTitle:@"连接" style:UIAlertActionStyleDefault handler:^(UIAlertAction *x) {
        NSString *u = [a.textFields.firstObject.text stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
        if (![u hasPrefix:@"http"]) u = [@"http://" stringByAppendingString:u];
        [[NSUserDefaults standardUserDefaults] setObject:u forKey:kURLKey];
        [self open:u];
    }]];
    if ([[NSUserDefaults standardUserDefaults] stringForKey:kURLKey].length)
        [a addAction:[UIAlertAction actionWithTitle:@"取消" style:UIAlertActionStyleCancel handler:nil]];
    [self presentViewController:a animated:YES completion:nil];
}
- (void)fail:(NSError *)e {
    if (e.code == NSURLErrorCancelled) return;
    UIAlertController *a = [UIAlertController alertControllerWithTitle:@"连不上" message:e.localizedDescription preferredStyle:UIAlertControllerStyleAlert];
    [a addAction:[UIAlertAction actionWithTitle:@"重试" style:UIAlertActionStyleDefault handler:^(UIAlertAction *x) { [self.web reload];
        if (!self.web.URL) [self open:[[NSUserDefaults standardUserDefaults] stringForKey:kURLKey]]; }]];
    [a addAction:[UIAlertAction actionWithTitle:@"改地址" style:UIAlertActionStyleDefault handler:^(UIAlertAction *x) { [self askURL]; }]];
    [self presentViewController:a animated:YES completion:nil];
}
- (void)webView:(WKWebView *)w didFailProvisionalNavigation:(WKNavigation *)n withError:(NSError *)e { [self fail:e]; }
- (void)webView:(WKWebView *)w didFailNavigation:(WKNavigation *)n withError:(NSError *)e { [self fail:e]; }

// JS alert / confirm / prompt (the console uses them)
- (void)webView:(WKWebView *)w runJavaScriptAlertPanelWithMessage:(NSString *)m initiatedByFrame:(WKFrameInfo *)f completionHandler:(void (^)(void))done {
    UIAlertController *a = [UIAlertController alertControllerWithTitle:nil message:m preferredStyle:UIAlertControllerStyleAlert];
    [a addAction:[UIAlertAction actionWithTitle:@"好" style:UIAlertActionStyleDefault handler:^(UIAlertAction *x) { done(); }]];
    [self presentViewController:a animated:YES completion:nil];
}
- (void)webView:(WKWebView *)w runJavaScriptConfirmPanelWithMessage:(NSString *)m initiatedByFrame:(WKFrameInfo *)f completionHandler:(void (^)(BOOL))done {
    UIAlertController *a = [UIAlertController alertControllerWithTitle:nil message:m preferredStyle:UIAlertControllerStyleAlert];
    [a addAction:[UIAlertAction actionWithTitle:@"取消" style:UIAlertActionStyleCancel handler:^(UIAlertAction *x) { done(NO); }]];
    [a addAction:[UIAlertAction actionWithTitle:@"确定" style:UIAlertActionStyleDefault handler:^(UIAlertAction *x) { done(YES); }]];
    [self presentViewController:a animated:YES completion:nil];
}
- (void)webView:(WKWebView *)w runJavaScriptTextInputPanelWithPrompt:(NSString *)p defaultText:(NSString *)d initiatedByFrame:(WKFrameInfo *)f completionHandler:(void (^)(NSString *))done {
    UIAlertController *a = [UIAlertController alertControllerWithTitle:nil message:p preferredStyle:UIAlertControllerStyleAlert];
    [a addTextFieldWithConfigurationHandler:^(UITextField *t) { t.text = d; }];
    [a addAction:[UIAlertAction actionWithTitle:@"取消" style:UIAlertActionStyleCancel handler:^(UIAlertAction *x) { done(nil); }]];
    [a addAction:[UIAlertAction actionWithTitle:@"确定" style:UIAlertActionStyleDefault handler:^(UIAlertAction *x) { done(a.textFields.firstObject.text); }]];
    [self presentViewController:a animated:YES completion:nil];
}
@end

@interface AppDelegate : UIResponder <UIApplicationDelegate>
@property (nonatomic, strong) UIWindow *window;
@end
@implementation AppDelegate
- (BOOL)application:(UIApplication *)app didFinishLaunchingWithOptions:(NSDictionary *)opts {
    self.window = [[UIWindow alloc] initWithFrame:UIScreen.mainScreen.bounds];
    self.window.rootViewController = [VC new];
    [self.window makeKeyAndVisible];
    return YES;
}
@end

int main(int argc, char *argv[]) {
    @autoreleasepool { return UIApplicationMain(argc, argv, nil, NSStringFromClass(AppDelegate.class)); }
}
