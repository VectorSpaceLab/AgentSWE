"""Trusted request/capture lineage; model metadata remains an untrusted claim."""
from pathlib import Path
import hashlib,json


ARTIFACT_SCHEMA_VERSION='agentswe-openhands-agent-result/v1'


def top_level_json_values(text):
    """Every complete top-level JSON value in text, and the offset decoding stopped at.

    A reply that carries a second complete object, or prose, after the first is not
    a syntax failure: both halves parse. Decode them one after another and report
    what could not be decoded, so the caller can decide and record the remainder.
    """
    decoder=json.JSONDecoder();values=[];index=0
    while index<len(text):
        while index<len(text) and text[index].isspace(): index+=1
        if index>=len(text): break
        try: parsed,end=decoder.raw_decode(text,index)
        except ValueError: break
        values.append((index,end,parsed));index=end
    return values,index


def artifact_from_concatenated(text):
    """The one authored artifact in a reply holding more than one top-level value.

    Returns (artifact, start, end, values) or None. None whenever nothing may be
    chosen on the model's behalf: a reply that is a single value with nothing after
    it (the strict parse already covers it), a reply where no top-level object
    carries the artifact schema_version the prompt asked for or a dict 'artifact'
    key, and a reply where MORE than one object does. The caller keeps its existing
    failure path in every one of those cases. The first object is never preferred
    just for being first: a terminal 'finish' choice emitted ahead of the artifact
    is exactly the shape this exists for.
    """
    values,stopped=top_level_json_values(text)
    if not values: return None
    if len(values)==1 and not text[stopped:].strip(): return None
    matches=[item for item in values if isinstance(item[2],dict)
             and (item[2].get('schema_version')==ARTIFACT_SCHEMA_VERSION or isinstance(item[2].get('artifact'),dict))]
    if len(matches)!=1: return None
    start,end,parsed=matches[0]
    return parsed,start,end,values


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path): return json.loads(Path(path).read_text())
def value(path):
    text=Path(path).read_text().strip()
    if text.startswith('```'): text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
    try:
        data=json.loads(text)
    except ValueError:
        # The capture deliberately keeps every original byte, so a reply that held
        # the artifact plus a second top-level object still has to bind here to the
        # artifact isolated_runtime parsed -- same rule, one definition.
        selected=artifact_from_concatenated(text)
        if selected is None: raise
        return selected[0]
    if not isinstance(data,dict): raise ValueError('completed model artifact must be a JSON object')
    return data


def decision(choice, actions):
    """Preserve a task-level terminal answer without inventing a product action."""
    kind=choice.get('kind')
    if kind=='finish' or (kind in {'complete','partial','blocked','conflict'} and 'action' not in choice):
        result=choice.get('decision') if kind=='finish' else kind
        if result not in {'complete','partial','blocked','conflict'}: raise ValueError('invalid terminal model decision')
        return {'kind':'terminal','decision':result,'raw_choice':choice}
    if kind!='action' or choice.get('action') not in actions:
        raise ValueError('model choice is neither a published action nor a terminal decision')
    args=choice.get('arguments',choice.get('args',{}))
    if not isinstance(args,dict): raise ValueError('model action arguments must be an object')
    return {'kind':'action','action':choice['action'],'arguments':args,'raw_choice':choice}


def completed_origin(lower, output, case_id, artifact, *, historical_proof=None):
    """Bind authored bytes to a completed request; never trust its echoed IDs."""
    output=Path(output); capture=output/'model_final_response.txt'
    if value(capture)!=artifact: raise ValueError('artifact differs from original completed model response')
    intent=read(str(capture)+'.request.json')
    if intent.get('state')!='completed' or intent.get('case_id')!=case_id or intent.get('phase')!='artifact' or intent.get('response_sha256')!=digest(capture):
        raise ValueError('final response lacks a matching completed local request intent')
    trajectory=read(output/'trajectory.json');steps=trajectory['steps']
    actual_digest=hashlib.sha256(lower.canonical_json(steps).encode()).hexdigest()
    if trajectory.get('trajectory_digest')!=actual_digest or trajectory.get('case_id')!=case_id: raise ValueError('captured trajectory identity mismatch')
    nonce=read(output/'private_world_config.json')['nonce'];nonce_digest=hashlib.sha256(lower.canonical_json(nonce).encode()).hexdigest()
    if historical_proof:
        proof=read(historical_proof)
        if proof.get('case_id')!=case_id or proof.get('capture_sha256')!=digest(capture) or proof.get('trajectory_sha256')!=digest(output/'trajectory.json'): raise ValueError('historical origin proof refers to other captured bytes')
        payload_path=Path(proof['payload_path']); payload=read(payload_path)
        broker=read(proof['broker_stats']); normalized=dict(payload)
        if isinstance(normalized['input'],str): normalized['input']=[{'type':'message','role':'user','content':[{'type':'input_text','text':normalized['input']}]}]
        normalized['model']=lower.MODEL;normalized['reasoning']={'effort':lower.EFFORT};normalized['stream']=True;normalized.pop('metadata',None);normalized.pop('reasoning_effort',None)
        reqsha=hashlib.sha256(json.dumps(normalized,sort_keys=True).encode()).hexdigest()
        upstream=broker.get('logical_requests',{}).get(reqsha,{})
        response_path=Path(upstream['response_path'])
        if response_path.parts[:2]==('/', 'evidence'): response_path=Path(proof['broker_stats']).parent / response_path.relative_to('/evidence')
        if upstream.get('state')!='completed' or digest(response_path)!=upstream.get('response_sha256'): raise ValueError('historical origin lacks a matching completed broker request')
        response=read(response_path);text=response.get('output_text') or ''.join(item.get('text','') for block in response.get('output',[]) for item in block.get('content',[]))
        if text!=capture.read_text(): raise ValueError('captured final output differs from broker completion')
        broker_proof={'request_sha256':reqsha,'response_id':response.get('id'),'response_sha256':upstream['response_sha256']}
    else:
        payload_path=Path(str(capture)+'.payload.json');payload=read(payload_path);broker_proof=None
        if intent.get('payload_sha256')!=digest(payload_path): raise ValueError('final request payload changed')
    if hashlib.sha256(json.dumps(payload).encode()).hexdigest()!=intent['request_sha256']: raise ValueError('final request payload is not the original local request')
    if payload.get('model')!=lower.MODEL or payload.get('reasoning')!={'effort':lower.EFFORT}: raise ValueError('actual lower model protocol differs')
    context=json.loads(payload['input'].rsplit('\n',1)[1])
    if context.get('actual_trajectory')!=steps or context.get('trajectory_digest')!=actual_digest or context.get('nonce_digest')!=nonce_digest: raise ValueError('final request was not made against the captured product world')
    return {'schema_version':'agentswe-openhands-captured-origin/v1','bound':True,'case_id':case_id,'model':lower.MODEL,'reasoning_effort':lower.EFFORT,'capture_sha256':digest(capture),'request_sha256':intent['request_sha256'],'payload_sha256':digest(payload_path),'trajectory_digest':actual_digest,'nonce_digest':nonce_digest,'broker_completion':broker_proof,'historical_proof':str(historical_proof) if historical_proof else None}
