// Client-side PWA and Web Push registration helper
(function () {
  function urlB64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  let swRegistration = null;

  if ('serviceWorker' in navigator && 'PushManager' in window) {
    window.addEventListener('load', async () => {
      try {
        swRegistration = await navigator.serviceWorker.register('/static/sw.js');
        initPushSubscription();
      } catch (err) {
        console.warn('Service worker registration failed:', err);
      }
    });
  }

  async function initPushSubscription() {
    if (!swRegistration) return;
    try {
      const response = await fetch('/push/vapid-public-key');
      if (!response.ok) return;
      const data = await response.json();
      if (!data.enabled || !data.public_key) return;

      const existingSub = await swRegistration.pushManager.getSubscription();
      const pushBtn = document.getElementById('enable-push-btn');

      if (existingSub) {
        if (pushBtn) pushBtn.style.display = 'none';
        return;
      }

      if (pushBtn) {
        pushBtn.style.display = 'inline-block';
        pushBtn.onclick = async () => {
          try {
            const subscription = await swRegistration.pushManager.subscribe({
              userVisibleOnly: true,
              applicationServerKey: urlB64ToUint8Array(data.public_key)
            });
            const subData = subscription.toJSON();
            await fetch('/push/subscribe', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                endpoint: subData.endpoint,
                keys: {
                  p256dh: subData.keys.p256dh,
                  auth: subData.keys.auth
                }
              })
            });
            pushBtn.style.display = 'none';
            alert('পুশ নোটিফিকেশন সফলভাবে চালু হয়েছে!');
          } catch (e) {
            console.error('Push subscribe error:', e);
            alert('নোটিফিকেশন চালু করা যায়নি। অনুগ্রহ করে ব্রাউজার পারমিশন পরীক্ষা করুন।');
          }
        };
      }
    } catch (err) {
      console.warn('Failed to initialize push subscription:', err);
    }
  }

  // PWA Install Prompt handling
  let deferredPrompt;
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredPrompt = e;
    const installBtn = document.getElementById('pwa-install-btn');
    if (installBtn) {
      installBtn.style.display = 'inline-block';
      installBtn.onclick = async () => {
        if (!deferredPrompt) return;
        deferredPrompt.prompt();
        const { outcome } = await deferredPrompt.userChoice;
        if (outcome === 'accepted') {
          installBtn.style.display = 'none';
        }
        deferredPrompt = null;
      };
    }
  });
})();
