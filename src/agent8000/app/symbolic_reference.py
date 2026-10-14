"""Bounded AST parser for elementary limits; never evals question/model text."""
import ast
import re

from .reference_tools import numbered_parts


def split_parts(text):
    matches=list(re.finditer(r'(?:^|[\n；;。])\s*(?:解\s*[：:]?\s*)?\$*\s*[（(]\s*(\d+)\s*[)）]',str(text)))
    return [(m.group(1),str(text)[m.end():matches[i+1].start() if i+1<len(matches) else len(text)].strip()) for i,m in enumerate(matches)]


def limit_answer(text):
    import sympy as s
    raw=str(text).strip().strip(';；。.$ ')
    match=re.match(r'lim_?\s*[({]\s*([xn])\s*(?:→|->)\s*([^)}]+)[)}]\s*(.+)',raw,re.S)
    if not match: return None
    variable, point, expression=match.groups()
    symbols={'x':s.Symbol('x',real=True),'a':s.Symbol('a',real=True),'n':s.Symbol('n',positive=True,integer=True)}
    functions={'sqrt':s.sqrt,'sin':s.sin,'cos':s.cos,'tan':s.tan,'log':s.log,'exp':s.exp,'factorial':s.factorial}
    def parse(value):
        value=value.strip().replace('−','-').replace('∞','oo').replace('^','**').replace('√(','sqrt(')
        value=re.sub(r'√([xna]|\d+)',r'sqrt(\1)',value)
        value=re.sub(r'\b(sin|cos|tan|log|exp)\s+([xna])\b',r'\1(\2)',value)
        value=re.sub(r'(\d)([xna])',r'\1*\2',value)
        value=re.sub(r'\((n\+1)\)!',r'factorial(\1)',value)
        value=re.sub(r'(\d+)!',r'factorial(\1)',value)
        if len(value)>500: raise ValueError('Expression too long')
        tree=ast.parse(value,mode='eval')
        if len(list(ast.walk(tree)))>120: raise ValueError('Expression too complex')
        def visit(node):
            if isinstance(node,ast.Expression): return visit(node.body)
            if isinstance(node,ast.Constant) and type(node.value) is int and abs(node.value)<=10000: return s.Integer(node.value)
            if isinstance(node,ast.Name) and node.id in symbols: return symbols[node.id]
            if isinstance(node,ast.Name) and node.id=='oo': return s.oo
            if isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.USub,ast.UAdd)):
                return -visit(node.operand) if isinstance(node.op,ast.USub) else visit(node.operand)
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id in functions and len(node.args)==1 and not node.keywords:
                arg=visit(node.args[0])
                if node.func.id=='factorial' and arg.is_number and abs(arg)>30: raise ValueError('Factorial too large')
                return functions[node.func.id](arg)
            if isinstance(node,ast.BinOp):
                left,right=visit(node.left),visit(node.right)
                if isinstance(node.op,ast.Add): return left+right
                if isinstance(node.op,ast.Sub): return left-right
                if isinstance(node.op,ast.Mult): return left*right
                if isinstance(node.op,ast.Div): return left/right
                if isinstance(node.op,ast.Pow) and right.is_Integer and abs(right)<=12: return left**right
            raise ValueError('Unsupported symbolic input')
        return visit(tree)
    try:
        target=parse(point)
        expr=expression.strip().strip('[]')
        if '...' in expr:
            if expr.startswith('(') and expr.endswith(')'): expr=expr[1:-1]
            terms=expr.replace(' ','').split('+...+')
            if len(terms)!=2 or target!=s.oo or variable!='n': return None
            first=terms[0].split('+')
            if len(first)!=2: return None
            nth=parse(terms[1]); n=symbols['n']
            if s.simplify(nth.subs(n,1)-parse(first[0]))!=0 or s.simplify(nth.subs(n,2)-parse(first[1]))!=0: return None
            value=s.summation(nth,(n,1,s.oo))
        else:
            expr=parse(expr)
            if target in (s.oo,-s.oo): value=s.limit(expr,symbols[variable],target)
            else:
                left=s.limit(expr,symbols[variable],target,dir='-');right=s.limit(expr,symbols[variable],target,dir='+')
                if left!=right: return None
                value=right
        if value.has(s.Limit,s.Sum,s.nan,s.zoo): return None
        return {'answer':s.sstr(value),'latex':s.latex(value),'verification':'sympy_from_question_text'}
    except (ValueError,TypeError,SyntaxError,NotImplementedError,RecursionError,ZeroDivisionError):
        return None


async def build_reference(problem, ask):
    parts=split_parts(problem.get('problem_text') or '')
    expected=numbered_parts(problem.get('problem_text'))
    if len(parts)<2: return None
    answers=[]
    for number,text in parts:
        symbolic=limit_answer(text)
        if symbolic:
            answers.append({'part_no':number,**symbolic,'solution':'由原题表达式独立进行符号极限/级数计算核验。'})
            continue
        result=await ask('/no_think\n独立求解下面一个数学小问。不接收学生作答，不参考其他题答案。'
            '不得猜测缺失题干；无把握时 complete=false。输出 JSON：'
            '{"complete":true,"answer":"","solution":"","risks":[]}。'
            +f'本题 {problem.get("problem_no")}，小问 ({number})：'+text,(),1800)
        answers.append({'part_no':number,'answer':str(result.get('answer') or ''),'solution':str(result.get('solution') or ''),
                        'verification':'model_generated_pending_review','complete':result.get('complete') is True and not result.get('risks')})
    usable=(set(expected)=={r['part_no'] for r in answers} and all(r['answer'] and (r.get('complete') or r['verification']=='sympy_from_question_text') for r in answers))
    return {'complete':usable,'usable':usable,'covered_parts':expected,'parts':answers,
            'answer':'\n'.join(f"({r['part_no']}) {r['answer']}" for r in answers),
            'solution':'\n'.join(f"({r['part_no']}) {r['solution']}" for r in answers),
            'source':'independent_reference_pending_teacher_review','risks':[] if usable else ['部分小问未可靠求解']}
