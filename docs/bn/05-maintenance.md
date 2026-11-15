# ৫. রক্ষণাবেক্ষণ ও সমস্যা সমাধান

## ব্যাকআপ

ব্যাকআপে ডাটাবেস (লগ, ঘটনা, নিবন্ধন, ব্যবহারকারী) আর মুখের নমুনার ছবি থাকে। অ্যাপ চলা অবস্থাতেই নিরাপদে নেওয়া যায়।

```powershell
.\scripts\backup.ps1
```

- ফাইল তৈরি হয় `backups\gate-face-<তারিখ>.zip` নামে।
- ডিফল্টভাবে সর্বশেষ **১৪টি** ব্যাকআপ রাখে, পুরোনোগুলো মুছে দেয়।
- অন্য ড্রাইভে ৩০টি রাখতে: `.\scripts\backup.ps1 -Keep 30 -Destination "E:\gate-face-backups"`

### প্রতিদিন নিজে থেকে ব্যাকআপ

অ্যাডমিন PowerShell-এ চালান। প্রতিদিন রাত ২টায় ব্যাকআপ হবে, পথ দুটো নিজের অনুযায়ী বদলে নিন:

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument '-NoProfile -ExecutionPolicy Bypass -File "D:\projetcs\gate\scripts\backup.ps1" -Destination "E:\gate-face-backups"' -WorkingDirectory "D:\projetcs\gate"
$trigger = New-ScheduledTaskTrigger -Daily -At 2am
Register-ScheduledTask -TaskName "GateFace Backup" -Action $action -Trigger $trigger -User "SYSTEM" -RunLevel Highest
```

> ব্যাকআপ **একই ডিস্কে রাখবেন না।** অন্য ড্রাইভ, NAS বা নিয়মিত পেনড্রাইভে কপি করুন। আর `.env` ফাইলের কপি ব্যাকআপের সাথে আলাদা জায়গায় রাখুন, এটা ছাড়া ব্যাকআপ থেকে মুখের ডাটা ফেরত পাওয়া যাবে না।

### ব্যাকআপ থেকে ফেরত আনা

1. সার্ভিস বন্ধ করুন: `Stop-Service GateFace`
2. ZIP খুলুন। ভেতরের `database\gate.db` ফাইলটি প্রজেক্টের `data\gate.db`-তে কপি করুন (আগেরটা বদলে), আর `data\faces\` ফোল্ডারটি প্রজেক্টের `data\faces\`-এ কপি করুন।
3. ব্যাকআপ নেওয়ার সময়ের **একই `.env`** থাকতে হবে।
4. সার্ভিস চালু করুন: `Start-Service GateFace`

## পুরোনো ডাটা নিজে থেকে মোছা

অ্যাপ প্রতিদিন একবার পুরোনো ডাটা মোছে:

| ডাটা | কতদিন থাকে | সেটিং |
|---|---|---|
| গেট লগের অচেনা মুখের ছবি | ৯০ দিন | `SNAPSHOT_RETENTION_DAYS` |
| গেট লগ ও ঘটনাবলির রেকর্ড | ৩৬৫ দিন | `EVENT_RETENTION_DAYS` |
| নজরদারির ছবি ও ভিডিও ক্লিপ | ৩০ দিন | `INCIDENT_MEDIA_RETENTION_DAYS` |

কী কী মুছবে, না মুছে আগে দেখতে চাইলে:

```powershell
.\.venv\Scripts\python.exe -m app.jobs.retention --dry-run
```

## ডিস্কের জায়গা

- **ভিডিও ক্লিপ:** প্রতি ক্যামেরায় প্রতি ঘণ্টা কার্যকলাপে প্রায় ০.০৪ GB।
- **হিসাবের উদাহরণ:** ৪টি ক্যামেরা × দিনে ৮ ঘণ্টা কার্যকলাপ × ৩০ দিন ≈ **৪০ GB**।
- ডিস্কে অন্তত **১০০ GB** খালি রাখুন।
- জায়গা দেখতে: `data\incidents\` ফোল্ডারের আকার দেখুন।

## সেটিংস (`.env` ফাইল)

`.env` বদলানোর পর অ্যাপ বা সার্ভিস রিস্টার্ট করতে হবে: `Restart-Service GateFace`

| সেটিং | ডিফল্ট | কাজ |
|---|---|---|
| `HOST` | `127.0.0.1` | `0.0.0.0` দিলে নেটওয়ার্কের অন্য কম্পিউটার থেকে খোলা যায় |
| `PORT` | `8090` | ওয়েব পোর্ট |
| `TZ` | `Asia/Dhaka` | সময় অঞ্চল |
| `MATCH_THRESHOLD` | `0.363` | মুখ মেলানোর কঠোরতা। বাড়ালে ভুল চেনা কমে, কিন্তু চেনা মানুষকেও অচেনা বলবে বেশি। |
| `MIN_FACE_PX` | `80` | এর চেয়ে ছোট মুখ বিচার করবে না |
| `DEBOUNCE_S` | `45` | একই মানুষের পরপর দুই এন্ট্রির মধ্যে ন্যূনতম সেকেন্ড |
| `SURVEILLANCE_ENABLED` | `true` | নজরদারি পুরোপুরি বন্ধ করতে `false` |
| `SURVEILLANCE_FPS` | `2.0` | প্রতি ক্যামেরায় সেকেন্ডে কয়টি ছবি বিশ্লেষণ। বেশি দিলে CPU বেশি লাগে। |
| `OBJECT_CONFIDENCE` | `0.45` | মানুষ বা গাড়ি শনাক্তের নিশ্চয়তা। বাড়ালে ভুল কমে, কিন্তু কিছু মিস হতে পারে। |
| `INCIDENT_IDLE_SECONDS` | `6` | কার্যকলাপ থামার কত সেকেন্ড পর ঘটনা শেষ ধরা হবে |
| `CLIP_MAX_SECONDS` | `60` | একটি ক্লিপের সর্বোচ্চ দৈর্ঘ্য |
| `CLIP_FPS` / `CLIP_MAX_WIDTH` | `4` / `640` | ক্লিপের মান। বাড়ালে ফাইল বড় হয়। |
| `COOKIE_SECURE` | `false` | HTTPS দিয়ে চালালে `true` |

## অ্যাপ আপডেট করা

1. ব্যাকআপ নিন।
2. সার্ভিস বন্ধ করুন: `Stop-Service GateFace`
3. নতুন কোড কপি করুন। `.env`, `data\` আর `models\` রেখে দিন।
4. প্যাকেজ হালনাগাদ করুন: `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
5. নতুন মডেল থাকলে নামান: `.\.venv\Scripts\python.exe -m tools.download_models`
6. সার্ভিস চালু করুন: `Start-Service GateFace`

ডাটাবেসের নতুন কলাম বা টেবিল অ্যাপ চালুর সময় নিজে থেকেই যোগ হয়।

## সাধারণ সমস্যা

| সমস্যা | সমাধান |
|---|---|
| অ্যাপ চালু হয় না, "Refusing to start with insecure settings" | `.env`-এ ডিফল্ট কী রয়ে গেছে। নতুন ইনস্টলে `python -m tools.generate_env` চালান। পুরোনো ইনস্টলে `.env`-এর ব্যাকআপ কপি ফেরত আনুন। |
| ব্রাউজারে পেজ খোলে না | সার্ভিস চলছে কি না দেখুন: `Get-Service GateFace`। ত্রুটি: `data\logs\service-error.log` |
| অন্য কম্পিউটার থেকে খোলে না | `.env`-এ `HOST=0.0.0.0` আছে কি না, আর Firewall নিয়ম ঠিক আছে কি না ([সেটআপ গাইড](01-setup.md), ধাপ ৬) |
| পাসওয়ার্ড ভুলে গেছেন | অন্য অ্যাডমিন **ব্যবহারকারী** পেজ থেকে রিসেট করতে পারেন। কেউ না থাকলে: `python -m app.createadmin` দিয়ে নতুন অ্যাডমিন বানান। |
| "অনেকবার ভুল চেষ্টা হয়েছে" | ১৫ মিনিট অপেক্ষা করুন, অথবা সার্ভিস রিস্টার্ট করুন |
| লগে "Camera … skipped: its saved URL cannot be decrypted" | `.env`-এর `FERNET_KEY` বদলে গেছে। পুরোনো `.env` ফেরত আনুন, অথবা ক্যামেরা এডিট করে URL ও পাসওয়ার্ড আবার দিন। |
| লগে "Surveillance disabled: Object model not found" | `python -m tools.download_models` চালিয়ে রিস্টার্ট করুন |
| ক্যামেরা offline | [ক্যামেরা গাইডের](02-cameras-hikvision.md) "সমস্যা হলে" অংশ দেখুন |
| পরিচিত কর্মীকে অচেনা বলছে | ওই গেটের ক্যামেরা দিয়ে আরও ৩–৫টি নমুনা নিন। মুখে আলো বাড়ান। |
| একজনকে অন্যজন বলে চিনছে | দুজনের নমুনাই দেখুন। দরকার হলে `MATCH_THRESHOLD` সামান্য বাড়ান (যেমন `0.40`)। |
| অনেক ভুল সতর্কতা | নিয়মের এলাকা ছোট করুন, সময়সূচি দিন, অথবা `OBJECT_CONFIDENCE` বাড়ান (যেমন `0.55`) |
| পিসি খুব ধীর | ক্যামেরা কমান, sub stream ব্যবহার করুন, অথবা `SURVEILLANCE_FPS=1` দিন |
| ঘটনার ক্লিপ নেই | `imageio-ffmpeg` ইনস্টল আছে কি না দেখুন: `pip install -r requirements.txt` |

## লগ ফাইল

| ফাইল | কী আছে |
|---|---|
| `data\logs\service-out.log` | সাধারণ বার্তা |
| `data\logs\service-error.log` | ত্রুটি ও সতর্কবার্তা, সমস্যা হলে প্রথমে এটা দেখুন |

লগে ক্যামেরার পাসওয়ার্ড কখনো লেখা হয় না, `***` দিয়ে ঢাকা থাকে।
