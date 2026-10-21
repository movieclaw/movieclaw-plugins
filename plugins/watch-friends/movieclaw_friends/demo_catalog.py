"""演示用的 80 部真实电影；虚拟人物与观看行为均由插件生成。

2026-10-20 通过正规只读 mclaw discover 逐部核验中文名、年份、片长、
类型、简短剧情和 TMDB 海报路径，并参考 TMDB 公开高分电影页面。
剧情仅保留来源资料的短概述；不保存宿主库存、账号、文件或凭据。
顺序按小映、阿川、小鹿、星子分为四个互不重叠的 20 部电影区块。
"""

FILMS = (
    # 小映：科幻与未来
    {
        "demo_id": "interstellar",
        "tmdb_id": 157336,
        "kind": "movie",
        "title": "星际穿越",
        "original_title": "Interstellar",
        "year": 2014,
        "runtime_minutes": 169,
        "genres": ["冒险", "剧情", "科幻"],
        "overview": (
            "太空探险队队员克服重重困难，成功通过新近发现的虫洞，突破人类"
            "太空旅行的极限，进入另一个时空。"
        ),
        "poster_path": "/spQm5r317XPNHK1941ChWmqzkZs.jpg",
        "source_url": "https://www.themoviedb.org/movie/157336?language=zh-CN",
    },
    {
        "demo_id": "inception",
        "tmdb_id": 27205,
        "kind": "movie",
        "title": "盗梦空间",
        "original_title": "Inception",
        "year": 2010,
        "runtime_minutes": 148,
        "genres": ["动作", "科幻", "犯罪"],
        "overview": (
            "道姆·柯布与同事阿瑟和纳什在一次针对日本能源大亨齐藤（渡边谦"
            " 饰）的盗梦行动中失败，反被齐藤利用。"
        ),
        "poster_path": "/89W962aAnPS3N3BdKgy2BvUhnCh.jpg",
        "source_url": "https://www.themoviedb.org/movie/27205?language=zh-CN",
    },
    {
        "demo_id": "tmdb-gad",
        "tmdb_id": 603,
        "kind": "movie",
        "title": "黑客帝国",
        "original_title": "The Matrix",
        "year": 1999,
        "runtime_minutes": 136,
        "genres": ["动作", "科幻"],
        "overview": ("不久的将来，网络黑客尼奥对这个看似正常的现实世界产生了怀疑。"),
        "poster_path": "/eMurN09rDC2qeEv3npkUbcJfIXN.jpg",
        "source_url": "https://www.themoviedb.org/movie/603?language=zh-CN",
    },
    {
        "demo_id": "tmdb-hi",
        "tmdb_id": 78,
        "kind": "movie",
        "title": "银翼杀手",
        "original_title": "Blade Runner",
        "year": 1982,
        "runtime_minutes": 117,
        "genres": ["科幻", "剧情", "惊悚"],
        "overview": (
            "在一个科技高度发达却因城市与社会凋敝而黯淡的未来，侦探瑞克·"
            "戴克奉命追捕叛逃的凶残复制人，同时被一位神秘女子吸引——她的"
            "秘密或许将侵蚀他的灵魂。"
        ),
        "poster_path": "/bLxQbj7fpVoVLX01qrYXpAD4jNP.jpg",
        "source_url": "https://www.themoviedb.org/movie/78?language=zh-CN",
    },
    {
        "demo_id": "tmdb-ddfjie",
        "tmdb_id": 335984,
        "kind": "movie",
        "title": "银翼杀手2049",
        "original_title": "Blade Runner 2049",
        "year": 2017,
        "runtime_minutes": 164,
        "genres": ["科幻", "剧情"],
        "overview": (
            "在《银翼杀手》（1982）事件发生三十年后，新一代银翼杀手、"
            "洛杉矶警察“K”发现了一个尘封已久的秘密，这个秘密有可能使所"
            "剩无几的社会陷入混乱。"
        ),
        "poster_path": "/sxA89XGotN9c5u5O1GWpbYYX3Ks.jpg",
        "source_url": "https://www.themoviedb.org/movie/335984?language=zh-CN",
    },
    {
        "demo_id": "tmdb-dei",
        "tmdb_id": 348,
        "kind": "movie",
        "title": "异形",
        "original_title": "Alien",
        "year": 1979,
        "runtime_minutes": 117,
        "genres": ["恐怖", "科幻"],
        "overview": ("未来的世界，人类已能轻松穿梭于星际之间，在太空中展开商业活动。"),
        "poster_path": "/62Z3qXj2X3b3f97CIjKaran2wAV.jpg",
        "source_url": "https://www.themoviedb.org/movie/348?language=zh-CN",
    },
    {
        "demo_id": "tmdb-ghj",
        "tmdb_id": 679,
        "kind": "movie",
        "title": "异形2",
        "original_title": "Aliens",
        "year": 1986,
        "runtime_minutes": 154,
        "genres": ["动作", "惊悚", "科幻"],
        "overview": (
            "在这部承接《异形》剧情的动作续集中，西格妮·韦弗回归饰演蕾普"
            "莉——人类首次遭遇异形怪物后的唯一幸存者。"
        ),
        "poster_path": "/vGOJQ3gsPiVxGWTaV7bVQUY7GoX.jpg",
        "source_url": "https://www.themoviedb.org/movie/679?language=zh-CN",
    },
    {
        "demo_id": "tmdb-gc",
        "tmdb_id": 62,
        "kind": "movie",
        "title": "2001太空漫游",
        "original_title": "2001: A Space Odyssey",
        "year": 1968,
        "runtime_minutes": 149,
        "genres": ["科幻", "悬疑", "冒险"],
        "overview": ("本片是一次通向未来的倒计时，一张指引人类命运的地图，一场对无限的求索。"),
        "poster_path": "/bO7J2LmtDYLyyBoOBeioYjp0VPl.jpg",
        "source_url": "https://www.themoviedb.org/movie/62?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bb",
        "tmdb_id": 11,
        "kind": "movie",
        "title": "星球大战4：新希望",
        "original_title": "Star Wars",
        "year": 1977,
        "runtime_minutes": 121,
        "genres": ["冒险", "动作", "科幻"],
        "overview": (
            "很久以前，在一个遥远的银河系……莱娅公主被邪恶的帝国势力挟为"
            "人质，企图镇压反抗银河帝国的起义军。"
        ),
        "poster_path": "/19RHmczYAPsvYw4OlLf0ZiV1IJM.jpg",
        "source_url": "https://www.themoviedb.org/movie/11?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bijb",
        "tmdb_id": 1891,
        "kind": "movie",
        "title": "星球大战5：帝国反击战",
        "original_title": "The Empire Strikes Back",
        "year": 1980,
        "runtime_minutes": 124,
        "genres": ["冒险", "动作", "科幻"],
        "overview": (
            "星球大战事件三年后，帝国军队继续追捕义军。义军在霍斯冰原星球"
            "战败后，卢克前往达戈巴星球，跟随自共和国覆灭后隐居的绝地大师"
            "尤达进行训练。为诱使卢克堕入黑暗面，…"
        ),
        "poster_path": "/unc5VVWPNTJlZP9oGDgL1dCE1vL.jpg",
        "source_url": "https://www.themoviedb.org/movie/1891?language=zh-CN",
    },
    {
        "demo_id": "tmdb-baf",
        "tmdb_id": 105,
        "kind": "movie",
        "title": "回到未来",
        "original_title": "Back to the Future",
        "year": 1985,
        "runtime_minutes": 116,
        "genres": ["冒险", "喜剧", "科幻"],
        "overview": (
            "马蒂·麦克弗莱，一位典型的八十年代美国青少年，意外被一台由钚"
            "动力驱动的德罗宁“时间机器”送回到1955年——这台机器由一"
            "位略带疯狂的科学家发明。"
        ),
        "poster_path": "/6fkVKq5LcZ6RJ4EaH84lJELDLJJ.jpg",
        "source_url": "https://www.themoviedb.org/movie/105?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bgf",
        "tmdb_id": 165,
        "kind": "movie",
        "title": "回到未来2",
        "original_title": "Back to the Future Part II",
        "year": 1989,
        "runtime_minutes": 108,
        "genres": ["冒险", "喜剧", "科幻"],
        "overview": (
            "马丁（Michael J. Fox 饰）才回到现实世界与女友"
            "团聚，布朗博士（Christopher Lloyd 饰）就心"
            "急火燎地把他们带到了30年后的未来世界，…"
        ),
        "poster_path": "/gUuzd7UqJyhFW69T6GTDXrmnfJN.jpg",
        "source_url": "https://www.themoviedb.org/movie/165?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bjg",
        "tmdb_id": 196,
        "kind": "movie",
        "title": "回到未来3",
        "original_title": "Back to the Future Part III",
        "year": 1990,
        "runtime_minutes": 119,
        "genres": ["冒险", "喜剧", "科幻"],
        "overview": (
            "马丁（麦克·J·福克斯饰）从他生活的八十年代来到了1955年"
            "，根据好朋友埃米·布朗博士（克里斯托弗·劳埃德饰）于1885"
            "年留下的信件，马丁找到了博士研制的时空穿梭机。"
        ),
        "poster_path": "/x2g9y6MHwuslZmDuKgrX1tRAbci.jpg",
        "source_url": "https://www.themoviedb.org/movie/196?language=zh-CN",
    },
    {
        "demo_id": "tmdb-cigcbh",
        "tmdb_id": 286217,
        "kind": "movie",
        "title": "火星救援",
        "original_title": "The Martian",
        "year": 2015,
        "runtime_minutes": 144,
        "genres": ["科幻", "剧情", "冒险"],
        "overview": (
            "载人航天宇宙飞船阿瑞斯3号成功抵达火星，谁知一场破坏力极其巨"
            "大的风暴向宇航员们袭来，阿瑞斯3号被迫中断任务，紧急返航。"
        ),
        "poster_path": "/4N94tiD7fLRHEl9z0OgruTBDorz.jpg",
        "source_url": "https://www.themoviedb.org/movie/286217?language=zh-CN",
    },
    {
        "demo_id": "tmdb-dcjigf",
        "tmdb_id": 329865,
        "kind": "movie",
        "title": "降临",
        "original_title": "Arrival",
        "year": 2016,
        "runtime_minutes": 116,
        "genres": ["剧情", "科幻", "悬疑"],
        "overview": (
            "当神秘的外星飞船在全球各地降落时，一支精英团队被召集起来展开"
            "调查——其中包括语言学家露易丝·班克斯。"
        ),
        "poster_path": "/9ontErpaTQ5m3EuAHiIElP90xjg.jpg",
        "source_url": "https://www.themoviedb.org/movie/329865?language=zh-CN",
    },
    {
        "demo_id": "tmdb-ejaeh",
        "tmdb_id": 49047,
        "kind": "movie",
        "title": "地心引力",
        "original_title": "Gravity",
        "year": 2013,
        "runtime_minutes": 91,
        "genres": ["科幻", "惊悚", "剧情"],
        "overview": (
            "瑞安·斯通博士首次执行太空任务，与经验丰富的宇航员马特·科沃"
            "斯基搭乘“探索者”号航天飞机，在太空中维修哈勃太空望远镜，而"
            "这也是科沃斯基退休前的最后一次飞行。"
        ),
        "poster_path": "/s8R23txlioy3OjAxXPAU0KZi9m.jpg",
        "source_url": "https://www.themoviedb.org/movie/49047?language=zh-CN",
    },
    {
        "demo_id": "tmdb-cgegga",
        "tmdb_id": 264660,
        "kind": "movie",
        "title": "机械姬",
        "original_title": "Ex Machina",
        "year": 2015,
        "runtime_minutes": 108,
        "genres": ["剧情", "科幻"],
        "overview": (
            "片中，伊萨克饰演一名神秘的亿万富翁，格里森饰演他公司的一名程"
            "序员，后者由于赢得公司一项幸运大奖而被邀请到老板的别墅共度周"
            "末。"
        ),
        "poster_path": "/cHwEZ228z5nbwjbiMWC4QMBljtq.jpg",
        "source_url": "https://www.themoviedb.org/movie/264660?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bdhbbd",
        "tmdb_id": 137113,
        "kind": "movie",
        "title": "明日边缘",
        "original_title": "Edge of Tomorrow",
        "year": 2014,
        "runtime_minutes": 113,
        "genres": ["动作", "科幻"],
        "overview": (
            "未来世界陷入外星人入侵的恐慌中，军事演说家凯奇少校畏惧上战场"
            "，他从没想过将军会让他去指挥即将开始的“诺曼底战役”，拒绝接"
            "受命令之后将军居然恶整了他，那就是把他当做逃兵扔进了军营中。"
        ),
        "poster_path": "/pBmA5w8lPl2eXW6FQ7Jgj0P2yI4.jpg",
        "source_url": "https://www.themoviedb.org/movie/137113?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bhedb",
        "tmdb_id": 17431,
        "kind": "movie",
        "title": "月球",
        "original_title": "Moon",
        "year": 2009,
        "runtime_minutes": 97,
        "genres": ["科幻", "剧情"],
        "overview": ("未来世界，随着科技的飞速进步，地球的污染也越来越严重。"),
        "poster_path": "/mKiBfGkcBu32FO21zTXIZ75vlvB.jpg",
        "source_url": "https://www.themoviedb.org/movie/17431?language=zh-CN",
    },
    {
        "demo_id": "tmdb-gig",
        "tmdb_id": 686,
        "kind": "movie",
        "title": "超时空接触",
        "original_title": "Contact",
        "year": 1997,
        "runtime_minutes": 150,
        "genres": ["剧情", "科幻", "悬疑"],
        "overview": ("艾莉·爱罗维（茱迪·福斯特 饰）自幼就对无线电通讯情有独钟。"),
        "poster_path": "/o440ZSUErzbGv0R2xnlloucO3hN.jpg",
        "source_url": "https://www.themoviedb.org/movie/686?language=zh-CN",
    },
    # 阿川：人生与故事
    {
        "demo_id": "truman-show",
        "tmdb_id": 37165,
        "kind": "movie",
        "title": "楚门的世界",
        "original_title": "The Truman Show",
        "year": 1998,
        "runtime_minutes": 103,
        "genres": ["喜剧", "剧情"],
        "overview": (
            "楚门·伯班克过着看似平凡而安稳的生活，却总觉得身边有些事情不"
            "太对劲：曾经突然消失的初恋、早已溺水身亡却似乎再次出现的父亲"
            "，以及一次次阻止他离开家乡的意外。"
        ),
        "poster_path": "/nAnzFcqORitpwvRQPceIt4mcm8G.jpg",
        "source_url": "https://www.themoviedb.org/movie/37165?language=zh-CN",
    },
    {
        "demo_id": "tmdb-chi",
        "tmdb_id": 278,
        "kind": "movie",
        "title": "肖申克的救赎",
        "original_title": "The Shawshank Redemption",
        "year": 1994,
        "runtime_minutes": 142,
        "genres": ["剧情", "犯罪"],
        "overview": (
            "1947年，银行家安迪·杜佛兰因被控杀害妻子及其情人，尽管始"
            "终坚称清白，仍被判处两项无期徒刑，送入缅因州的肖申克监狱服刑"
            "。"
        ),
        "poster_path": "/aAdnwqwkKX5PPcr8EdtaiA8AZVl.jpg",
        "source_url": "https://www.themoviedb.org/movie/278?language=zh-CN",
    },
    {
        "demo_id": "tmdb-cdi",
        "tmdb_id": 238,
        "kind": "movie",
        "title": "教父",
        "original_title": "The Godfather",
        "year": 1972,
        "runtime_minutes": 175,
        "genres": ["剧情", "犯罪"],
        "overview": (
            "40年代的美国，“教父”维托·唐·柯里昂是黑手党柯里昂家族的"
            "首领，带领家族从事非法的勾当，但同时他也是许多弱小平民的保护"
            "神，深得人们爱戴。"
        ),
        "poster_path": "/y03tzUKvkRCYwJ5NWys4W4bnS9m.jpg",
        "source_url": "https://www.themoviedb.org/movie/238?language=zh-CN",
    },
    {
        "demo_id": "tmdb-cea",
        "tmdb_id": 240,
        "kind": "movie",
        "title": "教父2",
        "original_title": "The Godfather Part II",
        "year": 1974,
        "runtime_minutes": 200,
        "genres": ["剧情", "犯罪"],
        "overview": ("作为《教父》的续篇，影片延续了科莱昂家族两代权力交替的史诗。"),
        "poster_path": "/4J8jUmjgKPggRPJqqdkjWCj2k0D.jpg",
        "source_url": "https://www.themoviedb.org/movie/240?language=zh-CN",
    },
    {
        "demo_id": "tmdb-ece",
        "tmdb_id": 424,
        "kind": "movie",
        "title": "辛德勒的名单",
        "original_title": "Schindler's List",
        "year": 1993,
        "runtime_minutes": 195,
        "genres": ["剧情", "历史", "战争"],
        "overview": (
            "1939年，纳粹德国入侵波兰。德国商人奥斯卡·辛德勒来到被占"
            "领的克拉科夫，利用自己的人脉和金钱开办了一家搪瓷厂，并雇用被"
            "迫劳动的犹太人生产军需用品，希望借战争获取财富。…"
        ),
        "poster_path": "/7cDI6MrIi12uQxfipiNIeJPYljy.jpg",
        "source_url": "https://www.themoviedb.org/movie/424?language=zh-CN",
    },
    {
        "demo_id": "tmdb-ffa",
        "tmdb_id": 550,
        "kind": "movie",
        "title": "搏击俱乐部",
        "original_title": "Fight Club",
        "year": 1999,
        "runtime_minutes": 139,
        "genres": ["剧情", "惊悚"],
        "overview": (
            "杰克( 爱德华·诺顿 Edward Norton 饰)是一个"
            "充满中年危机意识的人，他非常憎恨自己的生活及一切，再加上他患"
            "有严重的失眠症，所以他常常参加各种团体咨询会，只为了能接触人"
            "群。"
        ),
        "poster_path": "/qkrH7rjYKU4KOwwIl6wF6H3ISvm.jpg",
        "source_url": "https://www.themoviedb.org/movie/550?language=zh-CN",
    },
    {
        "demo_id": "tmdb-gia",
        "tmdb_id": 680,
        "kind": "movie",
        "title": "低俗小说",
        "original_title": "Pulp Fiction",
        "year": 1994,
        "runtime_minutes": 154,
        "genres": ["惊悚", "犯罪", "喜剧"],
        "overview": (
            "由序幕、尾声及三个独立又环环相扣的故事构成。早餐店两名劫匪打"
            "劫时，偶遇黑道打手朱尔斯与文森特；文森特奉命陪伴老大马沙的妻"
            "子，深陷诱惑困境；拳击手布奇因祖传金表，和马沙结下恩怨。"
        ),
        "poster_path": "/WZW5PCtCbtyKfojdLlPCi89DNx.jpg",
        "source_url": "https://www.themoviedb.org/movie/680?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bd",
        "tmdb_id": 13,
        "kind": "movie",
        "title": "阿甘正传",
        "original_title": "Forrest Gump",
        "year": 1994,
        "runtime_minutes": 142,
        "genres": ["喜剧", "剧情", "爱情"],
        "overview": (
            "阿甘出生在美国南方阿拉巴马州一个闭塞的小镇，智商只有75，然"
            "而他的妈妈是一位性格坚强的女性，她常常鼓励阿甘要自强不息。"
        ),
        "poster_path": "/xGEKFYixC7L4Mjhkq4dTwhmBlRI.jpg",
        "source_url": "https://www.themoviedb.org/movie/13?language=zh-CN",
    },
    {
        "demo_id": "tmdb-ejh",
        "tmdb_id": 497,
        "kind": "movie",
        "title": "绿里奇迹",
        "original_title": "The Green Mile",
        "year": 1999,
        "runtime_minutes": 189,
        "genres": ["奇幻", "剧情", "犯罪"],
        "overview": (
            "1935年。美国南部惨淡肃杀的冷山监狱。这里有片一英里长的绿"
            "地，人们叫它“绿里”。不过，它的居民皆为死囚，在绿地的另一头"
            "，便是行刑用的电椅。保罗·艾治科姆（汤姆·汉 克斯饰）是这里"
            "的狱监，…"
        ),
        "poster_path": "/oHL5har21XS91tAL0gUnDCg9kvD.jpg",
        "source_url": "https://www.themoviedb.org/movie/497?language=zh-CN",
    },
    {
        "demo_id": "tmdb-hgj",
        "tmdb_id": 769,
        "kind": "movie",
        "title": "好家伙",
        "original_title": "GoodFellas",
        "year": 1990,
        "runtime_minutes": 145,
        "genres": ["剧情", "犯罪"],
        "overview": (
            "本片根据真实故事改编，讲述了纽约黑手党血腥的江湖恩怨，把所谓"
            "的“江湖道义”还原为人性的丑陋与贪婪。"
        ),
        "poster_path": "/4W6gXVoNJKBCx2X1Jh5BQ17SAGN.jpg",
        "source_url": "https://www.themoviedb.org/movie/769?language=zh-CN",
    },
    {
        "demo_id": "tmdb-dij",
        "tmdb_id": 389,
        "kind": "movie",
        "title": "十二怒汉",
        "original_title": "12 Angry Men",
        "year": 1957,
        "runtime_minutes": 96,
        "genres": ["剧情"],
        "overview": ("西德尼·吕美特执导的本片堪称电影史上最颠覆的法庭剧。"),
        "poster_path": "/tbhKU2naM5ItPO4ICdF2GKow9ik.jpg",
        "source_url": "https://www.themoviedb.org/movie/389?language=zh-CN",
    },
    {
        "demo_id": "tmdb-gdh",
        "tmdb_id": 637,
        "kind": "movie",
        "title": "美丽人生",
        "original_title": "La vita è bella",
        "year": 1997,
        "runtime_minutes": 116,
        "genres": ["喜剧", "剧情"],
        "overview": ("1939年的意大利，乐观幽默的犹太青年圭多邂逅了女教师多拉。"),
        "poster_path": "/84PPFpTTMO83bPy19v7JgdNklDp.jpg",
        "source_url": "https://www.themoviedb.org/movie/637?language=zh-CN",
    },
    {
        "demo_id": "tmdb-hd",
        "tmdb_id": 73,
        "kind": "movie",
        "title": "美国X档案",
        "original_title": "American History X",
        "year": 1998,
        "runtime_minutes": 119,
        "genres": ["剧情"],
        "overview": (
            "德瑞克 的父亲在他很小的时候就被一名黑人毒贩射杀，从此给他幼小的心灵埋下了仇恨的种子。"
        ),
        "poster_path": "/sGh5KZWZEJCCSPZ9G9m5utuBhps.jpg",
        "source_url": "https://www.themoviedb.org/movie/73?language=zh-CN",
    },
    {
        "demo_id": "tmdb-be",
        "tmdb_id": 14,
        "kind": "movie",
        "title": "美国丽人",
        "original_title": "American Beauty",
        "year": 1999,
        "runtime_minutes": 122,
        "genres": ["剧情"],
        "overview": (
            "被禁忌的欲望所激荡，莱斯特·伯纳姆（凯文·史派西 饰）决定打破自己循规蹈矩的生活。"
        ),
        "poster_path": "/lzQSq5TDstu0J9KVkXmqbrVDs1k.jpg",
        "source_url": "https://www.themoviedb.org/movie/14?language=zh-CN",
    },
    {
        "demo_id": "tmdb-eij",
        "tmdb_id": 489,
        "kind": "movie",
        "title": "心灵捕手",
        "original_title": "Good Will Hunting",
        "year": 1997,
        "runtime_minutes": 126,
        "genres": ["剧情"],
        "overview": (
            "麻省理工学院的数学教授蓝波在席上公布了一道困难的数学题，却被"
            "年轻的清洁工威尔（马特·戴蒙 饰）解了出来。"
        ),
        "poster_path": "/oAZXx2Zt8Ikrm3BpLMsXYHRabWM.jpg",
        "source_url": "https://www.themoviedb.org/movie/489?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bbce",
        "tmdb_id": 1124,
        "kind": "movie",
        "title": "致命魔术",
        "original_title": "The Prestige",
        "year": 2006,
        "runtime_minutes": 130,
        "genres": ["剧情", "悬疑", "科幻"],
        "overview": (
            "改编自克里斯托弗·普瑞斯特的同名小说。19世纪末的伦敦，两位"
            "魔术师罗伯特·安吉尔与阿尔弗雷德·伯登因一场意外反目成仇，为"
            "争夺魔术界的至高荣誉，他们不断较量，逐渐陷入充满欺骗与危险的"
            "对决。"
        ),
        "poster_path": "/5DMDkeRfgISRqwxgrX9McAYwu6A.jpg",
        "source_url": "https://www.themoviedb.org/movie/1124?language=zh-CN",
    },
    {
        "demo_id": "tmdb-hh",
        "tmdb_id": 77,
        "kind": "movie",
        "title": "记忆碎片",
        "original_title": "Memento",
        "year": 2000,
        "runtime_minutes": 113,
        "genres": ["悬疑", "惊悚"],
        "overview": (
            "警方不予受理后，伦纳德·谢尔比执意追查强奸杀害妻子的凶手。然"
            "而，一种罕见且无法治愈的失忆症，使他踏上复仇之路难上加难。"
        ),
        "poster_path": "/rO9d5ES2v5h2xDDZzVWUfQwdZ42.jpg",
        "source_url": "https://www.themoviedb.org/movie/77?language=zh-CN",
    },
    {
        "demo_id": "tmdb-iah",
        "tmdb_id": 807,
        "kind": "movie",
        "title": "七宗罪",
        "original_title": "Se7en",
        "year": 1995,
        "runtime_minutes": 127,
        "genres": ["犯罪", "悬疑", "惊悚"],
        "overview": (
            "“暴食”、“贪婪”、“懒惰”、“嫉妒”、“骄傲”、“淫欲”、"
            "“愤怒”，这是天主教教义所指的人性七宗罪。"
        ),
        "poster_path": "/e7J95YHcmLtyUtB1yojY76xQO90.jpg",
        "source_url": "https://www.themoviedb.org/movie/807?language=zh-CN",
    },
    {
        "demo_id": "tmdb-dbddgj",
        "tmdb_id": 313369,
        "kind": "movie",
        "title": "爱乐之城",
        "original_title": "La La Land",
        "year": 2016,
        "runtime_minutes": 128,
        "genres": ["喜剧", "剧情", "爱情"],
        "overview": (
            "心怀演员梦想的米娅在试镜间隙为电影明星端送拿铁，而执着于爵士"
            "乐的塞巴斯汀则在昏暗酒吧靠弹奏钢琴曲勉强度日。"
        ),
        "poster_path": "/wEOWf7ahe1yeDkng1AxIcuIXcB.jpg",
        "source_url": "https://www.themoviedb.org/movie/313369?language=zh-CN",
    },
    {
        "demo_id": "tmdb-ceehig",
        "tmdb_id": 244786,
        "kind": "movie",
        "title": "爆裂鼓手",
        "original_title": "Whiplash",
        "year": 2014,
        "runtime_minutes": 107,
        "genres": ["剧情", "音乐", "惊悚"],
        "overview": (
            "年轻的爵士鼓手安德鲁·奈曼就读于全国顶尖的音乐学院，师从令人"
            "畏惧的爵士乐导师特伦斯·弗莱彻，在成为顶尖爵士鼓手的道路上奋"
            "力挣扎。"
        ),
        "poster_path": "/wOBIYg8cJUNumlBGyUk47Sw4olf.jpg",
        "source_url": "https://www.themoviedb.org/movie/244786?language=zh-CN",
    },
    # 小鹿：动画与成长
    {
        "demo_id": "spirited-away",
        "tmdb_id": 129,
        "kind": "movie",
        "title": "千与千寻",
        "original_title": "千と千尋の神隠し",
        "year": 2001,
        "runtime_minutes": 125,
        "genres": ["动画", "家庭", "奇幻"],
        "overview": (
            "10岁的千寻跟随父母搬家途中，意外穿过一条神秘隧道，闯入了一个属于神灵的奇异世界。"
        ),
        "poster_path": "/dnwMqbndJpOEhoVqjlMU9U7LFle.jpg",
        "source_url": "https://www.themoviedb.org/movie/129?language=zh-CN",
    },
    {
        "demo_id": "soul",
        "tmdb_id": 508442,
        "kind": "movie",
        "title": "心灵奇旅",
        "original_title": "Soul",
        "year": 2020,
        "runtime_minutes": 101,
        "genres": ["动画", "家庭", "剧情"],
        "overview": ("乔·加德纳一名中学乐队教师，终于获得在顶级爵士俱乐部演出的机会。"),
        "poster_path": "/yhyY94umEO1XkaxJAZJSMJk9hgt.jpg",
        "source_url": "https://www.themoviedb.org/movie/508442?language=zh-CN",
    },
    {
        "demo_id": "coco",
        "tmdb_id": 354912,
        "kind": "movie",
        "title": "寻梦环游记",
        "original_title": "Coco",
        "year": 2017,
        "runtime_minutes": 105,
        "genres": ["家庭", "动画", "音乐"],
        "overview": (
            "热爱音乐的少年米格出生在一个世代禁止族人接触音乐的家庭，却始"
            "终梦想成为偶像德拉库斯那样出色的音乐家。"
        ),
        "poster_path": "/vtgPOrShQFkDm1PWY3TKrPmlS5i.jpg",
        "source_url": "https://www.themoviedb.org/movie/354912?language=zh-CN",
    },
    {
        "demo_id": "wall-e",
        "tmdb_id": 10681,
        "kind": "movie",
        "title": "机器人总动员",
        "original_title": "WALL·E",
        "year": 2008,
        "runtime_minutes": 98,
        "genres": ["动画", "家庭", "科幻"],
        "overview": (
            "公元2805年，地球因严重的环境污染和堆积如山的垃圾早已不再"
            "适合人类居住，人类乘坐巨型飞船离开家园，只留下大批垃圾清理机"
            "器人继续工作。"
        ),
        "poster_path": "/eVcHArmmMXajXaqVBH4dhUWfwmS.jpg",
        "source_url": "https://www.themoviedb.org/movie/10681?language=zh-CN",
    },
    {
        "demo_id": "up",
        "tmdb_id": 14160,
        "kind": "movie",
        "title": "飞屋环游记",
        "original_title": "Up",
        "year": 2009,
        "runtime_minutes": 96,
        "genres": ["动画", "喜剧", "家庭"],
        "overview": ("小男孩卡尔从小怀揣着对冒险的向往，并因共同的梦想结识了活泼的艾莉。"),
        "poster_path": "/bnsWG5uFKquBUFfRCTcoJcm3qOH.jpg",
        "source_url": "https://www.themoviedb.org/movie/14160?language=zh-CN",
    },
    {
        "demo_id": "tmdb-igc",
        "tmdb_id": 862,
        "kind": "movie",
        "title": "玩具总动员",
        "original_title": "Toy Story",
        "year": 1995,
        "runtime_minutes": 81,
        "genres": ["家庭", "喜剧", "动画"],
        "overview": (
            "在胡迪的带领下，安迪的玩具们一直在他的房间里快乐地生活着，直"
            "到安迪的生日将巴斯光年带入这个大家庭。"
        ),
        "poster_path": "/tXAhMF48GYcmzhm45Iw4GDjNnB9.jpg",
        "source_url": "https://www.themoviedb.org/movie/862?language=zh-CN",
    },
    {
        "demo_id": "tmdb-igd",
        "tmdb_id": 863,
        "kind": "movie",
        "title": "玩具总动员2",
        "original_title": "Toy Story 2",
        "year": 1999,
        "runtime_minutes": 92,
        "genres": ["动画", "喜剧", "家庭"],
        "overview": ("胡迪和巴斯光年等一班玩具们经历了上回的历险后友谊日益增强。"),
        "poster_path": "/8BnAcoGbLvya0x4b0t3n8YBkUT0.jpg",
        "source_url": "https://www.themoviedb.org/movie/863?language=zh-CN",
    },
    {
        "demo_id": "tmdb-babjd",
        "tmdb_id": 10193,
        "kind": "movie",
        "title": "玩具总动员3",
        "original_title": "Toy Story 3",
        "year": 2010,
        "runtime_minutes": 103,
        "genres": ["动画", "家庭", "喜剧"],
        "overview": (
            "安迪已经高中毕业了，即将成为大学生，而他的玩具，包括胡迪、巴"
            "斯光年都面临将要被放在阁楼上的命运。"
        ),
        "poster_path": "/wNzvuGYoGl0LdWdGeCUXQ47jNVQ.jpg",
        "source_url": "https://www.themoviedb.org/movie/10193?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bc",
        "tmdb_id": 12,
        "kind": "movie",
        "title": "海底总动员",
        "original_title": "Finding Nemo",
        "year": 2003,
        "runtime_minutes": 100,
        "genres": ["动画", "家庭", "冒险"],
        "overview": ("主角是一对可爱的小丑鱼(Clownfish)父子。"),
        "poster_path": "/7nXuxfAyDwPrqSlqogLuNqGJJZ.jpg",
        "source_url": "https://www.themoviedb.org/movie/12?language=zh-CN",
    },
    {
        "demo_id": "tmdb-fif",
        "tmdb_id": 585,
        "kind": "movie",
        "title": "怪兽电力公司",
        "original_title": "Monsters, Inc.",
        "year": 2001,
        "runtime_minutes": 92,
        "genres": ["动画", "喜剧", "家庭"],
        "overview": (
            "我们不知道，在那样的世界里住着那样一群怪物。他们长相滑稽，却"
            "必须装作狰狞。因为，在夜深的时候，他们要悄悄的出现在孩子的衣"
            "橱里，吓唬刚刚甜美睡去的宝宝。一切，只因为他们是怪物电力公司"
            "的员工，…"
        ),
        "poster_path": "/fc5SCUbpO3aVBuzPpugoFPQ8K1n.jpg",
        "source_url": "https://www.themoviedb.org/movie/585?language=zh-CN",
    },
    {
        "demo_id": "tmdb-jiag",
        "tmdb_id": 9806,
        "kind": "movie",
        "title": "超人总动员",
        "original_title": "The Incredibles",
        "year": 2004,
        "runtime_minutes": 115,
        "genres": ["动作", "冒险", "动画"],
        "overview": ("鲍勃是一个超人特工，他惩恶扬善，深受街坊邻里的爱戴。"),
        "poster_path": "/9itP38fD0srB6pIAODUY4cVFYzk.jpg",
        "source_url": "https://www.themoviedb.org/movie/9806?language=zh-CN",
    },
    {
        "demo_id": "tmdb-cagc",
        "tmdb_id": 2062,
        "kind": "movie",
        "title": "美食总动员",
        "original_title": "Ratatouille",
        "year": 2007,
        "runtime_minutes": 111,
        "genres": ["动画", "喜剧", "家庭"],
        "overview": (
            "尽管雷米拥有惊人的嗅觉和敏锐的味觉，但他成为厨师的梦想似乎遥"
            "不可及，原因只有一个——他是一只老鼠！"
        ),
        "poster_path": "/drL61q25osRkgftiJjcHtdo7m7d.jpg",
        "source_url": "https://www.themoviedb.org/movie/2062?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bfafea",
        "tmdb_id": 150540,
        "kind": "movie",
        "title": "头脑特工队",
        "original_title": "Inside Out",
        "year": 2015,
        "runtime_minutes": 95,
        "genres": ["动画", "家庭", "冒险"],
        "overview": (
            "11岁的莱莉原本在明尼苏达州过着快乐而熟悉的生活，直到父亲因为工作带着全家搬到旧金山。"
        ),
        "poster_path": "/gN3qUaxucFWNTBDsaxkyCQSbox2.jpg",
        "source_url": "https://www.themoviedb.org/movie/150540?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bajeef",
        "tmdb_id": 109445,
        "kind": "movie",
        "title": "冰雪奇缘",
        "original_title": "Frozen",
        "year": 2013,
        "runtime_minutes": 102,
        "genres": ["动画", "家庭", "冒险"],
        "overview": ("在四面环海、风景如画的阿伦黛尔王国，生活着艾莎和安娜两位公主。"),
        "poster_path": "/eXGZGoKae2HFQ3ADCRmQChM2h14.jpg",
        "source_url": "https://www.themoviedb.org/movie/109445?language=zh-CN",
    },
    {
        "demo_id": "tmdb-cgjbej",
        "tmdb_id": 269149,
        "kind": "movie",
        "title": "疯狂动物城",
        "original_title": "Zootopia",
        "year": 2016,
        "runtime_minutes": 109,
        "genres": ["动画", "冒险", "家庭"],
        "overview": (
            "在所有哺乳动物共同生活的现代都市“疯狂动物城”，兔子朱迪从小就梦想成为一名警察。"
        ),
        "poster_path": "/6L0nYQqqEYPaSMjmsWH8dr8I4Sk.jpg",
        "source_url": "https://www.themoviedb.org/movie/269149?language=zh-CN",
    },
    {
        "demo_id": "tmdb-cgafbd",
        "tmdb_id": 260513,
        "kind": "movie",
        "title": "超人总动员2",
        "original_title": "Incredibles 2",
        "year": 2018,
        "runtime_minutes": 118,
        "genres": ["动作", "冒险", "动画"],
        "overview": (
            "深受喜爱的超人家族将在《超人总动员2》中强势回归，只不过这次"
            "站在聚光灯下的是弹力女超人海伦，而超能先生巴鲍勃则在家照料巴"
            "小倩和巴小飞，过起了“正常人”的居家生活。"
        ),
        "poster_path": "/ghWcicdO6so4iKe5QwyVFqInYtX.jpg",
        "source_url": "https://www.themoviedb.org/movie/260513?language=zh-CN",
    },
    {
        "demo_id": "tmdb-cadfc",
        "tmdb_id": 20352,
        "kind": "movie",
        "title": "神偷奶爸",
        "original_title": "Despicable Me",
        "year": 2010,
        "runtime_minutes": 95,
        "genres": ["动画", "喜剧", "犯罪"],
        "overview": (
            "超级坏蛋格鲁 在听说埃及金字塔被盗的新闻后不满市面上新贼辈出"
            "，决定在纳法利欧博士 的帮助下建造火箭升空盗取月亮。"
        ),
        "poster_path": "/dTJMcjnkTBHOzKaN71GE62zSgCi.jpg",
        "source_url": "https://www.themoviedb.org/movie/20352?language=zh-CN",
    },
    {
        "demo_id": "tmdb-babjb",
        "tmdb_id": 10191,
        "kind": "movie",
        "title": "驯龙高手",
        "original_title": "How to Train Your Dragon",
        "year": 2010,
        "runtime_minutes": 98,
        "genres": ["奇幻", "冒险", "动画"],
        "overview": (
            "维京岛国的少年小嗝嗝（杰伊•巴鲁切尔 Jay Baruche"
            "l 配音）是部落统领伟大的斯托里克（杰拉德·巴特勒 Gera"
            "rd Butler 配音）的儿子，…"
        ),
        "poster_path": "/2rNDBSL0xJO17oy8CZfOW8QjGKV.jpg",
        "source_url": "https://www.themoviedb.org/movie/10191?language=zh-CN",
    },
    {
        "demo_id": "tmdb-iai",
        "tmdb_id": 808,
        "kind": "movie",
        "title": "怪物史瑞克",
        "original_title": "Shrek",
        "year": 2001,
        "runtime_minutes": 90,
        "genres": ["动画", "喜剧", "奇幻"],
        "overview": (
            "从前，在一片遥远的沼泽里，住着一个脾气暴躁的怪物史莱克（迈克"
            "·迈尔斯 配音）。他珍贵的独居生活，突然被一群恼人的童话人物"
            "入侵而彻底打乱。[梦工厂]"
        ),
        "poster_path": "/40bL9rf34UsinhmlyuiG7OjqPO6.jpg",
        "source_url": "https://www.themoviedb.org/movie/808?language=zh-CN",
    },
    {
        "demo_id": "tmdb-ifih",
        "tmdb_id": 8587,
        "kind": "movie",
        "title": "狮子王",
        "original_title": "The Lion King",
        "year": 1994,
        "runtime_minutes": 89,
        "genres": ["动画", "家庭", "剧情"],
        "overview": ("辛巴是荣耀王国的小王子，他的父亲穆法沙是一个威严的国王。"),
        "poster_path": "/6f3r34trWZrZzRyTDdMzQp5XRrQ.jpg",
        "source_url": "https://www.themoviedb.org/movie/8587?language=zh-CN",
    },
    # 星子：东亚光影
    {
        "demo_id": "tmdb-dhcafi",
        "tmdb_id": 372058,
        "kind": "movie",
        "title": "你的名字。",
        "original_title": "君の名は。",
        "year": 2016,
        "runtime_minutes": 106,
        "genres": ["动画", "爱情", "剧情"],
        "overview": (
            "生活在深山小镇的高中女生宫水三叶一直向往着东京的生活，而住在"
            "东京的高中男生立花泷也过着属于自己的忙碌日常。"
        ),
        "poster_path": "/vRQ7wpxpHt2aV8RnOkwHlOQAMKe.jpg",
        "source_url": "https://www.themoviedb.org/movie/372058?language=zh-CN",
    },
    {
        "demo_id": "tmdb-fgibga",
        "tmdb_id": 568160,
        "kind": "movie",
        "title": "天气之子",
        "original_title": "天気の子",
        "year": 2019,
        "runtime_minutes": 111,
        "genres": ["动画", "剧情", "奇幻"],
        "overview": (
            "影片发生在天气错乱、阴雨连绵的时代。离家出走、独自上京闯荡的"
            "少年森岛帆高，遇到了同样生存艰难的少女天野阳菜。两人在大城市"
            "中相互依赖，并萌生情愫。拥有超能力的“100%晴天女孩”阳菜"
            "，…"
        ),
        "poster_path": "/e5dY456pcoBBNBnlSsTsbypGMj1.jpg",
        "source_url": "https://www.themoviedb.org/movie/568160?language=zh-CN",
    },
    {
        "demo_id": "tmdb-ejdf",
        "tmdb_id": 4935,
        "kind": "movie",
        "title": "哈尔的移动城堡",
        "original_title": "ハウルの動く城",
        "year": 2004,
        "runtime_minutes": 119,
        "genres": ["奇幻", "动画", "冒险"],
        "overview": (
            "18岁少女苏菲在街上遇到神秘青年霍尔，霍尔忽然抱起苏菲一同飞"
            "上天空，苏菲的心从此深深地被霍尔吸引。"
        ),
        "poster_path": "/mOkZtsb8UObRAa77EDTmbPB5BuY.jpg",
        "source_url": "https://www.themoviedb.org/movie/4935?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bci",
        "tmdb_id": 128,
        "kind": "movie",
        "title": "幽灵公主",
        "original_title": "もののけ姫",
        "year": 1997,
        "runtime_minutes": 134,
        "genres": ["冒险", "奇幻", "动画"],
        "overview": (
            "室町时代，生活在偏远村落的少年阿席达卡为了保护族人，与一头被"
            "邪魔侵蚀的山猪神交战，却因此受到可怕的诅咒。"
        ),
        "poster_path": "/mgxuL4rDqAHdcZvO3fXRhrcifpv.jpg",
        "source_url": "https://www.themoviedb.org/movie/128?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bafbf",
        "tmdb_id": 10515,
        "kind": "movie",
        "title": "天空之城",
        "original_title": "天空の城ラピュタ",
        "year": 1986,
        "runtime_minutes": 125,
        "genres": ["冒险", "奇幻", "动画"],
        "overview": (
            "古老帝国拉普达是一座漂浮在空中的巨大的机器岛，传说那里已经无人居住，蕴藏着巨大的财富。"
        ),
        "poster_path": "/evy3OHJrqWEPnkeBNzpPAAylG5K.jpg",
        "source_url": "https://www.themoviedb.org/movie/10515?language=zh-CN",
    },
    {
        "demo_id": "tmdb-idjc",
        "tmdb_id": 8392,
        "kind": "movie",
        "title": "龙猫",
        "original_title": "となりのトトロ",
        "year": 1988,
        "runtime_minutes": 86,
        "genres": ["奇幻", "动画", "家庭"],
        "overview": ("小月和四岁的妹妹小梅随父亲搬到乡间生活，以便离因病住院的母亲更近。"),
        "poster_path": "/pwDrh5XM59ULSFEsaUlJgt0WsOA.jpg",
        "source_url": "https://www.themoviedb.org/movie/8392?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bgifj",
        "tmdb_id": 16859,
        "kind": "movie",
        "title": "魔女宅急便",
        "original_title": "魔女の宅急便",
        "year": 1989,
        "runtime_minutes": 103,
        "genres": ["动画", "家庭", "奇幻"],
        "overview": (
            "琪琪今年13岁了，按照魔法界的规矩，魔法少女年满13岁就要出外进行为期一年的修行。"
        ),
        "poster_path": "/oFouL0QtqVjGP6JutSCYpY4AM5b.jpg",
        "source_url": "https://www.themoviedb.org/movie/16859?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bbgcb",
        "tmdb_id": 11621,
        "kind": "movie",
        "title": "红猪",
        "original_title": "紅の豚",
        "year": 1992,
        "runtime_minutes": 94,
        "genres": ["动画", "冒险", "奇幻"],
        "overview": (
            "20世纪20年代末的亚得里亚海，一战时期的王牌飞行员波鲁克不知何故变成了猪的模样。"
        ),
        "poster_path": "/yZRLvn5bJmavhpUPfRqAMqbrKCd.jpg",
        "source_url": "https://www.themoviedb.org/movie/11621?language=zh-CN",
    },
    {
        "demo_id": "tmdb-dhhjh",
        "tmdb_id": 37797,
        "kind": "movie",
        "title": "侧耳倾听",
        "original_title": "耳をすませば",
        "year": 1995,
        "runtime_minutes": 111,
        "genres": ["动画", "剧情", "家庭"],
        "overview": (
            "正在读初三的月岛雯是个热爱阅读的女孩，她常常在借书卡上看到“"
            "天泽圣司”这个名字，因此对这个素未谋面的人产生了好奇。"
        ),
        "poster_path": "/hPFcJ5S9KxZKN8u0FJ7OYvbQWaL.jpg",
        "source_url": "https://www.themoviedb.org/movie/37797?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bcehh",
        "tmdb_id": 12477,
        "kind": "movie",
        "title": "萤火虫之墓",
        "original_title": "火垂るの墓",
        "year": 1988,
        "runtime_minutes": 89,
        "genres": ["动画", "剧情", "战争"],
        "overview": ("1945年，第二次世界大战接近尾声，日本神户在空袭中化为一片火海。"),
        "poster_path": "/u8fiQxH8BZKARS2Y9vrnBygOCzi.jpg",
        "source_url": "https://www.themoviedb.org/movie/12477?language=zh-CN",
    },
    {
        "demo_id": "tmdb-djgfdf",
        "tmdb_id": 396535,
        "kind": "movie",
        "title": "釜山行",
        "original_title": "부산행",
        "year": 2016,
        "runtime_minutes": 118,
        "genres": ["动作", "恐怖", "惊悚"],
        "overview": ("证券公司基金管理人石宇（孔刘 饰）光鲜精干，却也是个重利轻义之徒。"),
        "poster_path": "/a31aMXYw3u2rfmzq4RDGvRtIMr1.jpg",
        "source_url": "https://www.themoviedb.org/movie/396535?language=zh-CN",
    },
    {
        "demo_id": "tmdb-cjaaji",
        "tmdb_id": 290098,
        "kind": "movie",
        "title": "小姐",
        "original_title": "아가씨",
        "year": 2016,
        "runtime_minutes": 145,
        "genres": ["惊悚", "剧情", "爱情"],
        "overview": (
            "故事背景设定在1930年代日本占领时期的朝鲜。新人女仆淑熙受"
            "雇服侍日本贵族秀子，后者与专横的舅舅居住在一座偏僻的乡间大宅"
            "中。然而女仆怀揣秘密：她实为一名扒手，受假冒日本伯爵的骗子招"
            "募，企图助其引诱…"
        ),
        "poster_path": "/4XSCTGx7qAkvcrFXMjrM1SpGPu0.jpg",
        "source_url": "https://www.themoviedb.org/movie/290098?language=zh-CN",
    },
    {
        "demo_id": "tmdb-ejgced",
        "tmdb_id": 496243,
        "kind": "movie",
        "title": "寄生虫",
        "original_title": "기생충",
        "year": 2019,
        "runtime_minutes": 132,
        "genres": ["喜剧", "惊悚", "剧情"],
        "overview": (
            "金基泽一家四口挤在首尔一间狭小的半地下室里，没有稳定工作，只"
            "能靠零散的活计勉强维持生活。"
        ),
        "poster_path": "/nwTQE4vWe6NoFSEgxGu4hO9J4Tv.jpg",
        "source_url": "https://www.themoviedb.org/movie/496243?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bbecd",
        "tmdb_id": 11423,
        "kind": "movie",
        "title": "杀人回忆",
        "original_title": "살인의 추억",
        "year": 2003,
        "runtime_minutes": 131,
        "genres": ["犯罪", "剧情", "惊悚"],
        "overview": (
            "在其突破性的第二部作品中，奉俊昊以颠覆性的手法引爆了警察类型"
            "片的传统桎梏，带来了震撼且超越类型的成果。"
        ),
        "poster_path": "/yVMRfewS90wuQHFKSKvf9vgjnUE.jpg",
        "source_url": "https://www.themoviedb.org/movie/11423?language=zh-CN",
    },
    {
        "demo_id": "tmdb-gha",
        "tmdb_id": 670,
        "kind": "movie",
        "title": "老男孩",
        "original_title": "올드보이",
        "year": 2003,
        "runtime_minutes": 120,
        "genres": ["惊悚", "悬疑"],
        "overview": (
            "女儿生日那天，中年男子吴大修（崔岷植饰）在醉酒回家路上，突遭不明身份的人绑架失踪。"
        ),
        "poster_path": "/3oQbLtBkYN4Y0Om7LiSqwg7l8Jv.jpg",
        "source_url": "https://www.themoviedb.org/movie/670?language=zh-CN",
    },
    {
        "demo_id": "tmdb-jffa",
        "tmdb_id": 9550,
        "kind": "movie",
        "title": "十面埋伏",
        "original_title": "十面埋伏",
        "year": 2004,
        "runtime_minutes": 119,
        "genres": ["冒险", "剧情", "动作"],
        "overview": (
            "唐大中十三年，民间涌现不少反昏君反腐官的组织，其中以打着“杀"
            "富济贫推翻朝廷”旗号的飞刀门的势力最大。"
        ),
        "poster_path": "/unOXxCipNht3h7RPhGy6OZKe5Gv.jpg",
        "source_url": "https://www.themoviedb.org/movie/9550?language=zh-CN",
    },
    {
        "demo_id": "tmdb-beg",
        "tmdb_id": 146,
        "kind": "movie",
        "title": "卧虎藏龙",
        "original_title": "卧虎藏龍",
        "year": 2000,
        "runtime_minutes": 120,
        "genres": ["爱情", "剧情", "动作"],
        "overview": (
            "十九世纪的中国，一位侠士将青冥剑赠予红颜知己，宝剑失窃后众人"
            "踏上寻剑之旅。追踪线索引向贝勒府，随着神秘刺客的出现与另一段"
            "爱情故事的展开，剧情走向陡然转折。"
        ),
        "poster_path": "/28FX5rCNo6p602DzgPOTNZjyMPh.jpg",
        "source_url": "https://www.themoviedb.org/movie/146?language=zh-CN",
    },
    {
        "demo_id": "tmdb-jeha",
        "tmdb_id": 9470,
        "kind": "movie",
        "title": "功夫",
        "original_title": "功夫",
        "year": 2004,
        "runtime_minutes": 99,
        "genres": ["动作", "喜剧", "犯罪"],
        "overview": (
            "20世纪40年代的上海，街头混混阿星自小受尽欺辱，一心想加入"
            "横行上海的黑道势力“斧头帮”，借此出人头地。"
        ),
        "poster_path": "/wv91QM70K9KzF9usPOebYX3LKkp.jpg",
        "source_url": "https://www.themoviedb.org/movie/9470?language=zh-CN",
    },
    {
        "demo_id": "tmdb-bbhha",
        "tmdb_id": 11770,
        "kind": "movie",
        "title": "少林足球",
        "original_title": "少林足球",
        "year": 2001,
        "runtime_minutes": 113,
        "genres": ["动作", "喜剧", "奇幻"],
        "overview": (
            "二十年前有“黄金右脚”之称，但因一场被师兄强雄设计陷害的比赛"
            "而断了右脚自此名誉扫地的明锋偶然认识了有惊人脚力的星，说服了"
            "对方练习足球参加不久会举行的全国超级杯足球大赛，…"
        ),
        "poster_path": "/rwfeeT02j1z5SBgJOH2eOwNCNQO.jpg",
        "source_url": "https://www.themoviedb.org/movie/11770?language=zh-CN",
    },
    {
        "demo_id": "tmdb-ied",
        "tmdb_id": 843,
        "kind": "movie",
        "title": "花样年华",
        "original_title": "花樣年華",
        "year": 2000,
        "runtime_minutes": 99,
        "genres": ["剧情", "爱情"],
        "overview": (
            "1962年的香港，报馆编辑周慕云与妻子搬进一间住满上海人的公"
            "寓，同一天搬来的还有在贸易公司工作的苏丽珍和她的丈夫。"
        ),
        "poster_path": "/1Z32pKrmWW2nkLFYGoc79MzJin6.jpg",
        "source_url": "https://www.themoviedb.org/movie/843?language=zh-CN",
    },
)
