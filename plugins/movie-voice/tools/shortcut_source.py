"""找电影快捷指令的可审阅源码；不读取任何本机凭据或服务器配置。"""
import uuid

PLACEHOLDER = '【必填】替换为你自己的 SEARCH_URL（由 movie-voice 插件生成）'
SHARE_SETUP = '''分享版｜未配置｜可公开转发

需要在使用者自己的 MovieClaw 服务端安装配套“寻映”插件 movie-voice。可从插件市场安装，或从插件仓库 Release 下载安装包。只安装手机快捷指令不够。只支持管理员本人。

唯一必填项：下方 SEARCH_URL 配置框。把占位文字整段替换为你自己生成的完整 search_url；不用另外填写服务器、账号、密码、管理员令牌或 PT Cookie。

从哪里获取：
1. 服务器地址：使用你平时登录自己 MovieClaw 网页的完整 HTTPS 地址（包含自定义端口）；例如 https://media.example.com。服务器管理员应先配置手机可访问的外部访问地址。
2. 服务端：设置 → 插件 → 安装寻映，或上传 movie-voice-0.1.0.mcplugin，按申请批准并启用。
3. 用自己的管理员账号登录 MovieClaw，打开侧栏“寻映”插件页面（也可在服务器地址后加 /apps/movie-voice）。
4. 页面可下载通用分享版；若“下载已配置个人版”已就绪，也可选择个人版，添加后直接使用，无需粘贴。个人版由这台服务器的管理员先在 Mac 签名并私密保存，不能公开分享。通用分享版始终保留占位。
5. 在同一页面点击“生成 SEARCH_URL”，再点“复制链接”。
6. 回到分享版快捷指令，把复制内容粘贴到下方 SEARCH_URL 配置框，替换整段占位文字。无需命令行。浏览器不允许自动复制时，可点“显示链接”后长按手动复制。

返回值范例（虚构，不可直接使用）：
{"search_url":"https://media.example.com/api/v1/plugins/movie-voice/shortcut/search?epoch=EXAMPLE_EPOCH_NOT_VALID&sig=EXAMPLE_SIGNATURE_NOT_VALID","revocable":true}

不要只填服务器首页，不要用管理员 API 令牌替代 epoch 或 sig，不要手工拼链接。修改后可把快捷指令改名为“找电影”；首次运行允许连接你自己的服务器。
配置后的文件含个人专用授权，不应再次公开分享。分享时请使用保留占位文字的原版。'''
HELP = '''找电影 · 配置与使用说明（v0.1.0）

配置：服务器安装“寻映”（movie-voice）插件（已验证 MovieClaw 0.41 / 0.42，管理员使用），批准搜索、订阅、通知及 agent:none 权限。智能纠错使用服务器已配置的模型；未配置时普通、同音和库存别名搜索仍可用。
用自己的管理员账号打开服务器上的“寻映”插件页面（/apps/movie-voice），点击“生成 SEARCH_URL” → “复制链接”，粘贴到本快捷指令顶部“文本”的 SEARCH_URL 配置框；个人版已填好。只替换这一框，其他动作无需改动。首次使用允许连接自己的服务器。

用法：说“嘿 Siri，找电影”，再说电影或电视剧名，可加年份。核对海报、片名、年份后选择并确认。列表底部可重新说、手动输入、补充导演/演员/剧情，或查看本说明。连续搜索最多 20 轮；取消系统输入或确认会结束。
已有电影或剧集：确认后向自己的 MovieClaw App 发观看通知；结果同时提供网页入口，未安装 App 时可登录网页观看。
缺少影片：电影确认后按服务器现有规则订阅并自动搜索下载；剧集先说出或输入明确的季数（如第1季、1,2，特别篇填0），再核对并确认。不默认全季，不自动续订未来新季。季号由服务器核验，不存在时不创建订阅。旧版快捷指令需要重新下载新版才能选择季数。找到资源的时间取决于线路和规则。
候选是临时影片卡，不写入通讯录。系统可能将影片卡称作“联系人”。

授权：无需把账号密码、管理员令牌或 PT Cookie 放入快捷指令。search_url 是可撤销的专用凭据，配置后的快捷指令请勿分享。对外只分享未配置的“找电影·分享版”。
需要撤销全部授权时，由服务器管理员执行：mclaw plugins movie-voice shortcut-revoke
撤销后重新配对并更新配置框。'''


def uid():return str(uuid.uuid4()).upper()
def text(value):return {'Value':{'string':value},'WFSerializationType':'WFTextTokenString'}
def ref(identifier,name='结果'):
    return {'Value':{'Type':'ActionOutput','OutputUUID':identifier,'OutputName':name},'WFSerializationType':'WFTextTokenAttachment'}
def variable(name):return {'Value':{'Type':'Variable','VariableName':name},'WFSerializationType':'WFTextTokenAttachment'}
def token(identifier,name='结果'):
    return {'Value':{'string':'\ufffc','attachmentsByRange':{'{0, 1}':ref(identifier,name)['Value']}},'WFSerializationType':'WFTextTokenString'}
def vartoken(name):return {'Value':{'string':'\ufffc','attachmentsByRange':{'{0, 1}':variable(name)['Value']}},'WFSerializationType':'WFTextTokenString'}
def dictionary(fields):
    return {'Value':{'WFDictionaryFieldValueItems':[
        {'WFKey':text(key),'WFItemType':0,'WFValue':value} for key,value in fields.items()]},'WFSerializationType':'WFDictionaryFieldValue'}

def workflow(shareable=False,test=False,search_url=None):
    if not shareable and not search_url:
        raise ValueError("个人版必须显式提供自己的 SEARCH_URL")
    if shareable and search_url is not None:
        raise ValueError("通用分享版禁止填入服务器授权")
    actions=[]
    help_text=(SHARE_SETUP+'\n\n'+HELP) if shareable else ('个人版｜已配置当前服务器｜请勿分享\n\nSEARCH_URL 已自动填好，无需复制粘贴。添加后可改名为“找电影”。授权撤销或服务器地址变更后，需要重新签名个人版。\n\n'+HELP)
    def add(name,**params):
        identifier=params.setdefault('UUID',uid());actions.append({'WFWorkflowActionIdentifier':'is.workflow.actions.'+name,'WFWorkflowActionParameters':params});return identifier
    def get(source,key):return add('getvalueforkey',WFInput=ref(source),WFDictionaryKey=key,WFGetDictionaryValueType='Value')
    def speak(source):return add('speaktext',WFText=token(source),WFSpeakTextWait=True,WFSpeakTextRate=.48)
    def save(source,name):return add('setvariable',WFInput=ref(source),WFVariableName=name)
    def condition(source,value):
        group=uid()
        typed=ref(source)
        typed['Value']['Aggrandizements']=[{'Type':'WFCoercionVariableAggrandizement','CoercionItemClass':'WFStringContentItem'}]
        add('conditional',WFControlFlowMode=0,GroupingIdentifier=group,
            WFInput={'Type':'Variable','Variable':typed},WFCondition=4,WFConditionalActionString=value)
        return group
    def end(group):add('conditional',WFControlFlowMode=2,GroupingIdentifier=group)
    add('comment',WFCommentActionText=help_text)
    add('comment',WFCommentActionText=('【唯一必填项】SEARCH_URL 配置框 ↓\n把下方占位文字整段替换为自己服务器插件页面生成并复制的 SEARCH_URL。格式范例与获取步骤见上方说明。不要粘贴整个 JSON，不要仅填写服务器首页。' if shareable else '【已配置】当前服务器专用 SEARCH_URL ↓\n无需修改。文件含个人授权，请勿分享；撤销授权或服务器地址变更后需重新签名。'))
    configured=PLACEHOLDER if shareable else search_url
    config=add('gettext',WFTextActionText=configured)
    # 用明确占位文字判断首次配置，避免系统把空文本对象也视为有值。
    initial=condition(config,PLACEHOLDER)
    add('showresult',Text=text('请先配置 SEARCH_URL。\n\n'+help_text));add('exit');end(initial)
    typed_config=ref(config)
    typed_config['Value']['Aggrandizements']=[{'Type':'WFCoercionVariableAggrandizement','CoercionItemClass':'WFStringContentItem'}]
    missing=uid();add('conditional',WFControlFlowMode=0,GroupingIdentifier=missing,
                     WFInput={'Type':'Variable','Variable':typed_config},WFCondition=101)
    add('showresult',Text=text('请先配置 SEARCH_URL。\n\n'+help_text));add('exit');end(missing)
    query=add('gettext',WFTextActionText='沙丘 2021') if test else add('ask',WFAskActionPrompt='想看哪部电影或电视剧？可以说片名加年份，也能输入文字。',WFInputType='Text')
    save(query,'电影名')
    empty=add('gettext',WFTextActionText='');save(empty,'补充线索')
    loop=uid();add('repeat.count',WFControlFlowMode=0,GroupingIdentifier=loop,WFRepeatCount=20)
    search=add('downloadurl',WFURL=token(config,'SEARCH_URL'),WFHTTPMethod='POST',WFJSONValues=dictionary({'query':vartoken('电影名'),'clues':vartoken('补充线索')}))
    message=get(search,'message');speak(message)
    vcard=get(search,'vcard');file=add('setitemname',WFInput=ref(vcard),WFName='影片候选.vcf')
    contacts=add('detect.contacts',WFInput=ref(file))
    selected=add('choosefromlist',WFInput=ref(contacts,'影片和搜索工具'),WFChooseFromListPrompt='核对海报、片名与年份；列表底部可重说或修改',WFChooseFromListSelectMultiple=False)
    address=add('properties.contacts',WFInput=ref(selected),WFContentItemPropertyName='URLs')
    detail=add('downloadurl',WFURL=token(address));kind=get(detail,'kind')
    group=condition(kind,'movie')
    save(detail,'确认结果')
    needs_seasons=get(detail,'needs_seasons');season_group=condition(needs_seasons,'yes')
    season_url=get(detail,'seasons_url')
    season_input=add('ask',WFAskActionPrompt='这是剧集。想订阅第几季？例如：第1季，或1,2；特别篇填0。不知道季数可取消，先在影片资料页核对。',WFInputType='Text')
    plan=add('downloadurl',WFURL=token(season_url),WFHTTPMethod='POST',WFJSONValues=dictionary({'seasons':token(season_input)}))
    save(plan,'确认结果');end(season_group)
    prompt,action,web=[add('getvalueforkey',WFInput=variable('确认结果'),WFDictionaryKey=key,WFGetDictionaryValueType='Value') for key in ('message','action_url','web_url')]
    add('alert',WFAlertActionTitle='确认影片',WFAlertActionMessage=token(prompt),WFAlertActionCancelButtonShown=True)
    result=add('downloadurl',WFURL=token(action),WFHTTPMethod='POST',WFJSONValues=dictionary({'confirm':text('true')}))
    final=get(result,'message');speak(final)
    display={'Value':{'string':'\ufffc\n\n网页观看入口（未入库时为空）：\n\ufffc','attachmentsByRange':{'{0, 1}':ref(final)['Value'],'{25, 1}':ref(web)['Value']}},'WFSerializationType':'WFTextTokenString'}
    # token 位置以实际字符串计算，避免中英文提示改动破坏魔法变量。
    st=display['Value']['string'];display['Value']['attachmentsByRange']={'{0, 1}':ref(final)['Value'],'{'+str(st.rfind('\ufffc'))+', 1}':ref(web)['Value']}
    add('showresult',Text=display);add('exit');end(group)
    for mode,prompt in [('voice','请重新说一次片名，可加年份。'),('manual','请用键盘手动输入正确片名，可加年份。'),('clues','补充年份、导演、演员或剧情，例如：诺兰导演，太空和黑洞。')]:
        group=condition(kind,mode)
        answer=add('ask',WFAskActionPrompt=prompt,WFInputType='Text',WFAskActionDefaultAnswer=vartoken('补充线索' if mode=='clues' else '电影名'))
        save(answer,'补充线索' if mode=='clues' else '电影名')
        if mode!='clues':save(empty,'补充线索')
        end(group)
    group=condition(kind,'help');add('showresult',Text=text(help_text));end(group)
    group=condition(kind,'exit');add('exit');end(group)
    add('repeat.count',WFControlFlowMode=2,GroupingIdentifier=loop)
    add('showresult',Text=text('本次已搜索 20 轮，请重新运行“找电影”继续。'))
    return {'WFWorkflowActions':actions,'WFWorkflowName':'找电影·分享版' if shareable else '找电影',
            'WFWorkflowClientVersion':'4011.0.5','WFWorkflowClientRelease':'26.0',
            'WFWorkflowMinimumClientVersion':900,'WFWorkflowMinimumClientVersionString':'900',
            'WFWorkflowIcon':{'WFWorkflowIconStartColor':1440408063,'WFWorkflowIconGlyphNumber':61508} if shareable else {'WFWorkflowIconStartColor':1440408063,'WFWorkflowIconGlyphNumber':61505},
            'WFWorkflowTypes':[],'WFWorkflowInputContentItemClasses':['WFStringContentItem'],'WFWorkflowImportQuestions':[]}
