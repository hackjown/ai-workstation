// AI Workstation — minimal iOS shell for the web console (TrollStore / unsigned).
// Full-screen WKWebView pointed at the API host. First launch asks for the URL,
// e.g. http://192.168.50.x:8000/?token=XXXX . Three-finger long-press to change it.
#import <UIKit/UIKit.h>
#import <WebKit/WebKit.h>

static NSString *const kURLKey = @"aiw_url";

@interface VC : UIViewController <WKNavigationDelegate, WKUIDelegate>
@property (nonatomic, strong) WKWebView *web;
@end

@implementation VC
- (void)viewDidLoad {
    [super viewDidLoad];
    self.view.backgroundColor = [UIColor colorWithRed:0.06 green:0.07 blue:0.08 alpha:1];
    WKWebViewConfiguration *cfg = [WKWebViewConfiguration new];
    cfg.allowsInlineMediaPlayback = YES;
    cfg.mediaTypesRequiringUserActionForPlayback = WKAudiovisualMediaTypeNone;
    cfg.websiteDataStore = [WKWebsiteDataStore defaultDataStore];
    self.web = [[WKWebView alloc] initWithFrame:self.view.bounds configuration:cfg];
    self.web.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
    self.web.navigationDelegate = self;
    self.web.UIDelegate = self;
    self.web.opaque = NO;
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
- (UIStatusBarStyle)preferredStatusBarStyle { return UIStatusBarStyleLightContent; }
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
